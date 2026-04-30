<?php

namespace Drupal\remedy\Controller;

use Drupal\Component\Utility\Html;
use Drupal\Core\Controller\ControllerBase;
use Drupal\node\NodeInterface;

/**
 * Right-side panel content.
 */
class PanelController extends ControllerBase {

  /**
   * Build the panel shell.
   */
  public function build(NodeInterface $node): array {
    return [
      '#type' => 'container',
      '#attributes' => [
        'class' => ['remedy-panel'],
        'data-remedy-node-id' => (string) $node->id(),
      ],
      'header' => [
        '#markup' => '<div class="remedy-panel__header"><h2>' . Html::escape($node->label()) . '</h2><div class="remedy-panel__meta">Accessibility issues</div></div>',
      ],
      'summary' => [
        '#markup' => '<div class="remedy-panel__summary" data-remedy-summary>Loading…</div>',
      ],
      'issues' => [
        '#markup' => '<div class="remedy-panel__issues" data-remedy-issues></div>',
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

}
