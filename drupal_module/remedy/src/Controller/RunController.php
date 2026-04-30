<?php

namespace Drupal\remedy\Controller;

use Drupal\Core\Controller\ControllerBase;
use Drupal\node\NodeInterface;
use Drupal\remedy\Service\BackendClient;
use Drupal\remedy\Service\IssueRepository;
use Symfony\Component\DependencyInjection\ContainerInterface;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;

/**
 * JSON endpoints for scan/fix/poll flows.
 */
class RunController extends ControllerBase {

  /**
   * Constructs the controller.
   */
  public function __construct(
    protected BackendClient $backendClient,
    protected IssueRepository $issueRepository,
  ) {}

  /**
   * {@inheritdoc}
   */
  public static function create(ContainerInterface $container): static {
    return new static(
      $container->get('remedy.backend_client'),
      $container->get('remedy.issue_repository'),
    );
  }

  /**
   * Return latest issue payload for a node.
   */
  public function issues(NodeInterface $node): JsonResponse {
    $payload = $this->backendClient->latestPageScan('node', (int) $node->id());
    if (!empty($payload['schema_version'])) {
      $this->issueRepository->ingestScanPayload($payload);
      $payload = $this->issueRepository->applyIssueStatesToPayload($payload);
    }
    return new JsonResponse($payload);
  }

  /**
   * Queue a scan.
   */
  public function queueScan(NodeInterface $node): JsonResponse {
    $response = $this->backendClient->queueScan('node', (int) $node->id(), $node->toUrl('canonical', ['absolute' => TRUE])->toString());
    if (!empty($response['run_id'])) {
      $this->issueRepository->recordQueuedScanRun(
        'node',
        (int) $node->id(),
        $node->label(),
        $node->toUrl('canonical', ['absolute' => TRUE])->toString(),
        (string) $response['run_id'],
        $this->currentUser()->id() ? (int) $this->currentUser()->id() : NULL,
        [
          'entity_uuid' => $node->uuid(),
          'bundle' => $node->bundle(),
          'published' => (int) $node->isPublished(),
        ],
      );
    }
    return new JsonResponse($response);
  }

  /**
   * Queue a fix run.
   */
  public function queueFix(NodeInterface $node, Request $request): JsonResponse {
    $mode = (string) ($request->request->get('mode') ?: 'traced');
    return new JsonResponse(
      $this->backendClient->queueFix('node', (int) $node->id(), $mode)
    );
  }

  /**
   * Poll a run.
   */
  public function poll(string $run_id): JsonResponse {
    $run = $this->backendClient->pollRun($run_id);
    if (($run['status'] ?? '') === 'completed') {
      if (!empty($run['payload']['schema_version'])) {
        $this->issueRepository->ingestScanPayload($run['payload'], $run_id);
        $run['payload'] = $this->issueRepository->applyIssueStatesToPayload($run['payload']);
      }
      if (!empty($run['result']['verify_payload']['schema_version'])) {
        $this->issueRepository->ingestScanPayload($run['result']['verify_payload']);
        $run['result']['verify_payload'] = $this->issueRepository->applyIssueStatesToPayload($run['result']['verify_payload']);
      }
    }
    return new JsonResponse($run);
  }

}
