<?php

namespace Drupal\remedy\Service;

use Drupal\Core\Config\ConfigFactoryInterface;
use Drupal\Core\Logger\LoggerChannelFactoryInterface;
use GuzzleHttp\ClientInterface;
use GuzzleHttp\Exception\GuzzleException;

/**
 * Thin HTTP client for the Remedy Drupal backend.
 */
class BackendClient {

  /**
   * Constructs the backend client.
   */
  public function __construct(
    protected ClientInterface $httpClient,
    protected ConfigFactoryInterface $configFactory,
    protected LoggerChannelFactoryInterface $loggerFactory,
  ) {}

  /**
   * Queue a scan run.
   */
  public function queueScan(string $entityType, int $entityId, ?string $canonicalUrl = NULL): array {
    $payload = [
      'site_code' => $this->siteCode(),
      'page' => [
        'entity_type' => $entityType,
        'entity_id' => $entityId,
        'canonical_url' => $canonicalUrl,
      ],
      'options' => [
        'include_content_accuracy' => (bool) $this->settings()->get('include_content_accuracy'),
        'interaction_profile' => (string) ($this->settings()->get('interaction_profile') ?: 'default'),
      ],
    ];
    return $this->request('POST', '/v1/pages/scan', ['json' => $payload]);
  }

  /**
   * Queue a fix run.
   */
  public function queueFix(string $entityType, int $entityId, string $mode = 'traced'): array {
    $payload = [
      'site_code' => $this->siteCode(),
      'page' => [
        'entity_type' => $entityType,
        'entity_id' => $entityId,
      ],
      'mode' => $mode,
    ];
    return $this->request('POST', '/v1/pages/fix', ['json' => $payload]);
  }

  /**
   * Poll a backend run.
   */
  public function pollRun(string $runId): array {
    return $this->request('GET', '/v1/runs/' . rawurlencode($runId));
  }

  /**
   * Fetch the latest page payload.
   */
  public function latestPageScan(string $entityType, int $entityId): array {
    $query = ['query' => ['site_code' => $this->siteCode()]];
    return $this->request('GET', sprintf('/v1/pages/%s/%d/latest', $entityType, $entityId), $query);
  }

  /**
   * Return the configured settings.
   */
  protected function settings() {
    return $this->configFactory->get('remedy.settings');
  }

  /**
   * Return the configured site code.
   */
  protected function siteCode(): string {
    return (string) ($this->settings()->get('site_code') ?: '');
  }

  /**
   * Perform a backend request and decode JSON.
   */
  protected function request(string $method, string $path, array $options = []): array {
    $headers = $options['headers'] ?? [];
    $token = (string) ($this->settings()->get('api_token') ?: '');
    if ($token !== '') {
      $headers['Authorization'] = 'Bearer ' . $token;
    }
    $headers['X-Remedy-Site'] = $this->siteCode();
    $headers['Accept'] = 'application/json';
    $options['headers'] = $headers;

    try {
      $response = $this->httpClient->request(
        $method,
        rtrim((string) $this->settings()->get('backend_base_url'), '/') . $path,
        $options,
      );
      return json_decode((string) $response->getBody(), TRUE) ?: [];
    }
    catch (GuzzleException $e) {
      $this->loggerFactory->get('remedy')->error('Backend request failed: @message', ['@message' => $e->getMessage()]);
      return [
        'status' => 'failed',
        'error' => $e->getMessage(),
      ];
    }
  }

}
