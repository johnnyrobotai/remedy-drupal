<?php

namespace Drupal\remedy\Controller;

use Drupal\Core\Controller\ControllerBase;
use Drupal\Core\Url;
use Drupal\node\NodeInterface;

/**
 * Full page report rendering.
 */
class PageReportController extends ControllerBase {

  /**
   * Title callback.
   */
  public function title(NodeInterface $node): string {
    return 'Accessibility: ' . $node->label();
  }

  /**
   * Build the admin report page.
   */
  public function build(NodeInterface $node): array {
    return [
      '#type' => 'container',
      '#attributes' => [
        'class' => ['remedy-page-report'],
        'data-remedy-node-id' => (string) $node->id(),
      ],
      'header' => [
        '#markup' => '<div class="remedy-page-report__header"><p>Use the side panel to inspect and highlight current issues for this node.</p></div>',
      ],
      'panel_link' => [
        '#type' => 'link',
        '#title' => $this->t('Open accessibility panel'),
        '#url' => Url::fromRoute('remedy.panel', ['node' => $node->id()], ['attributes' => ['class' => ['use-ajax']]]),
      ],
      '#attached' => [
        'library' => ['remedy/panel'],
        'drupalSettings' => [
          'remedy' => [
            'panel' => [
              'nodeId' => (int) $node->id(),
              'issuesUrl' => '/remedy/page/' . $node->id() . '/issues',
              'scanUrl' => '/remedy/page/' . $node->id() . '/scan',
              'fixUrl' => '/remedy/page/' . $node->id() . '/fix',
            ],
          ],
        ],
      ],
    ];
  }

  /**
   * Build the node-local tab page.
   */
  public function nodeTab(NodeInterface $node): array {
    return $this->build($node);
  }

}
