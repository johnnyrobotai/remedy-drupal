<?php

namespace Drupal\remedy\Service;

use Drupal\Core\Database\Connection;

/**
 * Query helpers and ingestion logic for Remedy tables.
 */
class IssueRepository {

  /**
   * Constructs the repository.
   */
  public function __construct(protected Connection $database) {}

  /**
   * Return recent tracked pages.
   */
  public function latestPages(int $limit = 50): array {
    return $this->database->select('remedy_page', 'p')
      ->fields('p')
      ->orderBy('last_scanned_at', 'DESC')
      ->range(0, $limit)
      ->execute()
      ->fetchAllAssoc('id');
  }

  /**
   * Return recent scan runs.
   */
  public function latestRuns(int $limit = 50): array {
    return $this->database->select('remedy_scan_run', 'r')
      ->fields('r')
      ->orderBy('created', 'DESC')
      ->range(0, $limit)
      ->execute()
      ->fetchAllAssoc('id');
  }

  /**
   * Store a queued scan run for a node.
   */
  public function recordQueuedScanRun(
    string $entityType,
    int $entityId,
    string $title,
    string $canonicalUrl,
    string $backendRunId,
    ?int $requestedByUid = NULL,
    array $pageDefaults = [],
  ): int {
    $pageId = $this->ensurePageRecord($entityType, $entityId, [
      'entity_uuid' => $pageDefaults['entity_uuid'] ?? NULL,
      'bundle' => $pageDefaults['bundle'] ?? NULL,
      'title' => $title,
      'canonical_url' => $canonicalUrl,
      'path_alias' => $pageDefaults['path_alias'] ?? NULL,
      'published' => $pageDefaults['published'] ?? 1,
    ]);

    $existingId = $this->lookupScanRunIdByBackendRunId($backendRunId);
    $fields = [
      'page_id' => $pageId,
      'backend_run_id' => $backendRunId,
      'trigger_type' => 'manual',
      'status' => 'queued',
      'engine' => 'axe-core',
      'requested_by_uid' => $requestedByUid,
      'created' => $this->now(),
    ];

    if ($existingId) {
      $this->database->update('remedy_scan_run')
        ->fields($fields)
        ->condition('id', $existingId)
        ->execute();
      return (int) $existingId;
    }

    return (int) $this->database->insert('remedy_scan_run')
      ->fields($fields)
      ->execute();
  }

  /**
   * Ingest a completed scan payload into local tables.
   */
  public function ingestScanPayload(array $payload, ?string $backendRunId = NULL, ?int $requestedByUid = NULL): array {
    $page = $payload['page'] ?? [];
    $run = $payload['run'] ?? [];
    if (empty($page['entity_type']) || empty($page['entity_id']) || empty($run['run_id'])) {
      return [];
    }

    $entityType = (string) $page['entity_type'];
    $entityId = (int) $page['entity_id'];
    $backendRunId = $backendRunId ?: (string) $run['run_id'];
    $completedAt = $this->timestampFromIso($run['completed_at'] ?? NULL) ?: $this->now();

    $transaction = $this->database->startTransaction();
    try {
      $pageId = $this->ensurePageRecord($entityType, $entityId, [
        'entity_uuid' => $page['entity_uuid'] ?? NULL,
        'bundle' => $page['bundle'] ?? NULL,
        'title' => $page['title'] ?? ('Entity ' . $entityId),
        'canonical_url' => $page['canonical_url'] ?? '',
        'path_alias' => $page['path_alias'] ?? NULL,
        'published' => 1,
      ]);

      $scanRunId = $this->upsertCompletedScanRun($pageId, $backendRunId, $payload, $requestedByUid);
      $issues = is_array($payload['issues'] ?? NULL) ? $payload['issues'] : [];
      $existingStates = $this->loadIssueStates($pageId);
      $seenFingerprints = [];

      $this->database->delete('remedy_issue_instance')
        ->condition('scan_run_id', $scanRunId)
        ->execute();

      foreach ($issues as $issue) {
        $fingerprint = (string) ($issue['fingerprint'] ?? '');
        if ($fingerprint === '') {
          continue;
        }
        $seenFingerprints[] = $fingerprint;
        $statusSnapshot = $this->upsertIssueState($pageId, $scanRunId, $issue, $existingStates, $completedAt);

        $this->database->insert('remedy_issue_instance')
          ->fields([
            'scan_run_id' => $scanRunId,
            'page_id' => $pageId,
            'fingerprint' => $fingerprint,
            'engine' => (string) ($issue['engine'] ?? 'axe-core'),
            'rule_id' => (string) ($issue['rule_id'] ?? ''),
            'rule_url' => $issue['rule_url'] ?? NULL,
            'category' => (string) ($issue['category'] ?? 'accessibility'),
            'impact' => (string) ($issue['impact'] ?? 'unknown'),
            'status_snapshot' => $statusSnapshot,
            'wcag_tags_json' => $this->encodeJson($issue['wcag_tags'] ?? []),
            'description' => (string) ($issue['description'] ?? ''),
            'help' => $issue['help'] ?? NULL,
            'help_url' => $issue['help_url'] ?? NULL,
            'failure_summary' => $issue['failure_summary'] ?? NULL,
            'review_state' => (string) ($issue['review_state'] ?? 'needs_review'),
            'occurrence_count' => (int) ($issue['occurrence_count'] ?? 1),
            'targets_json' => $this->encodeJson($issue['targets'] ?? []),
            'html_snippet' => $issue['primary_target']['html_snippet'] ?? NULL,
            'trace_json' => $this->encodeJson($issue['trace'] ?? []),
            'fixability' => (string) ($issue['fixability'] ?? 'unknown'),
            'created' => $completedAt,
          ])
          ->execute();
      }

      $this->markMissingIssuesFixed($pageId, $scanRunId, $seenFingerprints, $completedAt);

      $this->database->update('remedy_page')
        ->fields([
          'last_scan_run_id' => $scanRunId,
          'last_scanned_at' => $completedAt,
          'last_issue_count' => count($issues),
          'last_status' => count($issues) ? 'issues_found' : 'passing',
          'changed' => $this->now(),
        ])
        ->condition('id', $pageId)
        ->execute();

      return [
        'page_id' => $pageId,
        'scan_run_id' => $scanRunId,
      ];
    }
    catch (\Throwable $e) {
      throw $e;
    }
  }

  /**
   * Overlay persisted issue state onto a live payload.
   */
  public function applyIssueStatesToPayload(array $payload): array {
    $page = $payload['page'] ?? [];
    if (empty($page['entity_type']) || empty($page['entity_id'])) {
      return $payload;
    }
    $pageId = $this->lookupPageId((string) $page['entity_type'], (int) $page['entity_id']);
    if (!$pageId) {
      return $payload;
    }

    $states = $this->loadIssueStates($pageId);
    $statusCounts = [];
    foreach (($payload['issues'] ?? []) as $index => $issue) {
      $fingerprint = (string) ($issue['fingerprint'] ?? '');
      if ($fingerprint === '' || !isset($states[$fingerprint])) {
        $status = (string) ($issue['status'] ?? 'open');
        $statusCounts[$status] = ($statusCounts[$status] ?? 0) + 1;
        continue;
      }
      $state = $states[$fingerprint];
      $payload['issues'][$index]['status'] = $state['current_status'];
      $payload['issues'][$index]['state'] = [
        'assigned_uid' => $state['assigned_uid'],
        'snoozed_until' => $this->isoFromTimestamp($state['snoozed_until']),
        'resolution_note' => $state['resolution_note'],
        'first_seen_at' => $this->isoFromTimestamp($state['first_seen_at']),
        'last_seen_at' => $this->isoFromTimestamp($state['last_seen_at']),
      ];
      $statusCounts[$state['current_status']] = ($statusCounts[$state['current_status']] ?? 0) + 1;
    }

    $payload['summary']['by_status'] = $statusCounts;
    return $payload;
  }

  /**
   * Ensure a page record exists and return its local ID.
   */
  protected function ensurePageRecord(string $entityType, int $entityId, array $fields): int {
    $existingId = $this->lookupPageId($entityType, $entityId);
    $values = [
      'entity_uuid' => $fields['entity_uuid'] ?? NULL,
      'bundle' => $fields['bundle'] ?? NULL,
      'title' => (string) ($fields['title'] ?? ('Entity ' . $entityId)),
      'canonical_url' => (string) ($fields['canonical_url'] ?? ''),
      'path_alias' => $fields['path_alias'] ?? NULL,
      'published' => (int) ($fields['published'] ?? 1),
      'changed' => $this->now(),
    ];

    if ($existingId) {
      $this->database->update('remedy_page')
        ->fields($values)
        ->condition('id', $existingId)
        ->execute();
      return $existingId;
    }

    return (int) $this->database->insert('remedy_page')
      ->fields($values + [
        'entity_type' => $entityType,
        'entity_id' => $entityId,
        'created' => $this->now(),
      ])
      ->execute();
  }

  /**
   * Find a page ID by entity reference.
   */
  protected function lookupPageId(string $entityType, int $entityId): ?int {
    $id = $this->database->select('remedy_page', 'p')
      ->fields('p', ['id'])
      ->condition('entity_type', $entityType)
      ->condition('entity_id', $entityId)
      ->execute()
      ->fetchField();
    return $id ? (int) $id : NULL;
  }

  /**
   * Find a local scan run by backend run ID.
   */
  protected function lookupScanRunIdByBackendRunId(string $backendRunId): ?int {
    $id = $this->database->select('remedy_scan_run', 'r')
      ->fields('r', ['id'])
      ->condition('backend_run_id', $backendRunId)
      ->execute()
      ->fetchField();
    return $id ? (int) $id : NULL;
  }

  /**
   * Insert or update a completed scan run row.
   */
  protected function upsertCompletedScanRun(int $pageId, string $backendRunId, array $payload, ?int $requestedByUid = NULL): int {
    $run = $payload['run'] ?? [];
    $summary = $payload['summary'] ?? [];
    $render = $payload['render'] ?? [];
    $runId = $this->lookupScanRunIdByBackendRunId($backendRunId);
    $startedAt = $this->timestampFromIso($run['started_at'] ?? NULL);
    $completedAt = $this->timestampFromIso($run['completed_at'] ?? NULL);
    $engine = 'axe-core';
    if (!empty($summary['engines'][0])) {
      $engine = (string) $summary['engines'][0];
    }

    $fields = [
      'page_id' => $pageId,
      'backend_run_id' => $backendRunId,
      'trigger_type' => (string) ($run['trigger_type'] ?? 'manual'),
      'status' => (string) ($run['status'] ?? 'completed'),
      'engine' => $engine,
      'started_at' => $startedAt,
      'completed_at' => $completedAt,
      'summary_json' => $this->encodeJson($summary),
      'render_json' => $this->encodeJson($render),
      'screenshot_uri' => $render['screenshot_url'] ?? NULL,
      'error_message' => NULL,
      'created' => $startedAt ?: $this->now(),
    ];

    if ($requestedByUid !== NULL) {
      $fields['requested_by_uid'] = $requestedByUid;
    }

    if ($runId) {
      $this->database->update('remedy_scan_run')
        ->fields($fields)
        ->condition('id', $runId)
        ->execute();
      return $runId;
    }

    return (int) $this->database->insert('remedy_scan_run')
      ->fields($fields)
      ->execute();
  }

  /**
   * Load issue states for a page keyed by fingerprint.
   */
  protected function loadIssueStates(int $pageId): array {
    $rows = $this->database->select('remedy_issue_state', 's')
      ->fields('s')
      ->condition('page_id', $pageId)
      ->execute()
      ->fetchAllAssoc('fingerprint', \PDO::FETCH_ASSOC);
    return is_array($rows) ? $rows : [];
  }

  /**
   * Insert or update current issue state and return snapshot status.
   */
  protected function upsertIssueState(int $pageId, int $scanRunId, array $issue, array &$existingStates, int $completedAt): string {
    $fingerprint = (string) ($issue['fingerprint'] ?? '');
    $now = $this->now();
    $payloadState = $issue['state'] ?? [];
    if (isset($existingStates[$fingerprint])) {
      $existing = $existingStates[$fingerprint];
      $currentStatus = (string) ($existing['current_status'] ?? 'open');
      if ($currentStatus === 'fixed') {
        $currentStatus = 'open';
      }
      $this->database->update('remedy_issue_state')
        ->fields([
          'current_status' => $currentStatus,
          'last_seen_run_id' => $scanRunId,
          'last_seen_at' => $completedAt,
          'changed' => $now,
        ])
        ->condition('id', $existing['id'])
        ->execute();
      $existingStates[$fingerprint]['current_status'] = $currentStatus;
      $existingStates[$fingerprint]['last_seen_run_id'] = $scanRunId;
      $existingStates[$fingerprint]['last_seen_at'] = $completedAt;
      $existingStates[$fingerprint]['changed'] = $now;
      return $currentStatus;
    }

    $currentStatus = (string) ($issue['status'] ?? 'open');
    $id = $this->database->insert('remedy_issue_state')
      ->fields([
        'page_id' => $pageId,
        'fingerprint' => $fingerprint,
        'current_status' => $currentStatus,
        'assigned_uid' => $payloadState['assigned_uid'] ?? NULL,
        'snoozed_until' => $this->timestampFromIso($payloadState['snoozed_until'] ?? NULL),
        'resolution_note' => $payloadState['resolution_note'] ?? NULL,
        'first_seen_run_id' => $scanRunId,
        'last_seen_run_id' => $scanRunId,
        'first_seen_at' => $this->timestampFromIso($payloadState['first_seen_at'] ?? NULL) ?: $completedAt,
        'last_seen_at' => $this->timestampFromIso($payloadState['last_seen_at'] ?? NULL) ?: $completedAt,
        'last_changed_by_uid' => NULL,
        'changed' => $now,
      ])
      ->execute();

    $existingStates[$fingerprint] = [
      'id' => $id,
      'page_id' => $pageId,
      'fingerprint' => $fingerprint,
      'current_status' => $currentStatus,
      'assigned_uid' => $payloadState['assigned_uid'] ?? NULL,
      'snoozed_until' => $this->timestampFromIso($payloadState['snoozed_until'] ?? NULL),
      'resolution_note' => $payloadState['resolution_note'] ?? NULL,
      'first_seen_run_id' => $scanRunId,
      'last_seen_run_id' => $scanRunId,
      'first_seen_at' => $this->timestampFromIso($payloadState['first_seen_at'] ?? NULL) ?: $completedAt,
      'last_seen_at' => $this->timestampFromIso($payloadState['last_seen_at'] ?? NULL) ?: $completedAt,
      'last_changed_by_uid' => NULL,
      'changed' => $now,
    ];
    return $currentStatus;
  }

  /**
   * Mark previously open issues as fixed when they disappear from a scan.
   */
  protected function markMissingIssuesFixed(int $pageId, int $scanRunId, array $seenFingerprints, int $completedAt): void {
    $query = $this->database->update('remedy_issue_state')
      ->fields([
        'current_status' => 'fixed',
        'last_seen_run_id' => $scanRunId,
        'last_seen_at' => $completedAt,
        'changed' => $this->now(),
      ])
      ->condition('page_id', $pageId)
      ->condition('current_status', 'fixed', '<>');

    if (!empty($seenFingerprints)) {
      $query->condition('fingerprint', $seenFingerprints, 'NOT IN');
    }

    $query->execute();
  }

  /**
   * Encode an array for blob storage.
   */
  protected function encodeJson(array $value): string {
    return json_encode($value, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE) ?: '[]';
  }

  /**
   * Convert an ISO timestamp to Unix time.
   */
  protected function timestampFromIso(?string $value): ?int {
    if (!$value) {
      return NULL;
    }
    $timestamp = strtotime($value);
    return $timestamp === FALSE ? NULL : (int) $timestamp;
  }

  /**
   * Convert a Unix timestamp back to ISO8601.
   */
  protected function isoFromTimestamp(?int $value): ?string {
    if (!$value) {
      return NULL;
    }
    return gmdate('c', $value);
  }

  /**
   * Current request time.
   */
  protected function now(): int {
    return \Drupal::time()->getRequestTime();
  }

}
