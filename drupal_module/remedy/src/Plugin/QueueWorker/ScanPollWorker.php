<?php

namespace Drupal\remedy\Plugin\QueueWorker;

use Drupal\Core\Queue\QueueWorkerBase;

/**
 * Polls queued Remedy scan runs.
 *
 * @QueueWorker(
 *   id = "remedy_scan_poll_worker",
 *   title = @Translation("Remedy scan poll worker"),
 *   cron = {"time" = 30}
 * )
 */
class ScanPollWorker extends QueueWorkerBase {

  /**
   * {@inheritdoc}
   */
  public function processItem($data): void {
    // Intentionally minimal in v1 scaffold.
    // The full implementation should poll the backend, persist results, and
    // reschedule incomplete runs.
  }

}

