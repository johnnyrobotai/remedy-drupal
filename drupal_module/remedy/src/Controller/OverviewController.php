<?php

namespace Drupal\remedy\Controller;

use Drupal\Core\Controller\ControllerBase;
use Drupal\Core\Link;
use Drupal\Core\Url;
use Drupal\remedy\Service\IssueRepository;
use Symfony\Component\DependencyInjection\ContainerInterface;

/**
 * Overview pages for Remedy.
 */
class OverviewController extends ControllerBase {

  /**
   * Constructs the controller.
   */
  public function __construct(protected IssueRepository $issueRepository) {}

  /**
   * {@inheritdoc}
   */
  public static function create(ContainerInterface $container): static {
    return new static(
      $container->get('remedy.issue_repository'),
    );
  }

  /**
   * Build the overview page.
   */
  public function build(): array {
    $rows = [];
    foreach ($this->issueRepository->latestPages() as $row) {
      $rows[] = [
        Link::fromTextAndUrl($row->title ?: ('Node ' . $row->entity_id), Url::fromRoute('remedy.page_report', ['node' => $row->entity_id])),
        $row->last_issue_count,
        $row->last_status,
        $row->last_scanned_at ? $this->dateFormatter()->format($row->last_scanned_at, 'short') : $this->t('Never'),
      ];
    }

    return [
      'intro' => [
        '#markup' => '<p>Remedy Drupal surfaces accessibility scans and fixes from the backend.</p>',
      ],
      'table' => [
        '#type' => 'table',
        '#header' => [$this->t('Page'), $this->t('Issues'), $this->t('Status'), $this->t('Last scanned')],
        '#rows' => $rows,
        '#empty' => $this->t('No tracked pages have been stored yet.'),
      ],
    ];
  }

  /**
   * Build the runs page.
   */
  public function runs(): array {
    $rows = [];
    foreach ($this->issueRepository->latestRuns() as $row) {
      $rows[] = [
        $row->id,
        $row->trigger_type,
        $row->status,
        $row->engine,
        $row->created ? $this->dateFormatter()->format($row->created, 'short') : '',
      ];
    }

    return [
      '#type' => 'table',
      '#header' => [$this->t('Run'), $this->t('Trigger'), $this->t('Status'), $this->t('Engine'), $this->t('Created')],
      '#rows' => $rows,
      '#empty' => $this->t('No scan runs have been stored yet.'),
    ];
  }

}
