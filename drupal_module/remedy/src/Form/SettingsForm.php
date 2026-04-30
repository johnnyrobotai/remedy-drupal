<?php

namespace Drupal\remedy\Form;

use Drupal\Core\Form\ConfigFormBase;
use Drupal\Core\Form\FormStateInterface;

/**
 * Remedy settings form.
 */
class SettingsForm extends ConfigFormBase {

  /**
   * {@inheritdoc}
   */
  protected function getEditableConfigNames(): array {
    return ['remedy.settings'];
  }

  /**
   * {@inheritdoc}
   */
  public function getFormId(): string {
    return 'remedy_settings_form';
  }

  /**
   * {@inheritdoc}
   */
  public function buildForm(array $form, FormStateInterface $form_state): array {
    $config = $this->config('remedy.settings');

    $form['backend_base_url'] = [
      '#type' => 'url',
      '#title' => $this->t('Backend base URL'),
      '#default_value' => $config->get('backend_base_url') ?: 'http://127.0.0.1:8787',
      '#required' => TRUE,
    ];

    $form['api_token'] = [
      '#type' => 'password',
      '#title' => $this->t('API token'),
      '#default_value' => '',
      '#required' => FALSE,
      '#description' => $this->t('Bearer token sent to the Remedy Drupal backend. Leave blank to keep the existing token.'),
      '#attributes' => [
        'autocomplete' => 'new-password',
      ],
    ];

    $form['api_token_clear'] = [
      '#type' => 'checkbox',
      '#title' => $this->t('Clear stored API token'),
      '#default_value' => FALSE,
    ];

    $form['site_code'] = [
      '#type' => 'textfield',
      '#title' => $this->t('Site code'),
      '#default_value' => $config->get('site_code') ?: '',
      '#required' => TRUE,
      '#description' => $this->t('Matches the Python backend site/campus code, for example ELAC.'),
    ];

    $form['interaction_profile'] = [
      '#type' => 'textfield',
      '#title' => $this->t('Default interaction profile'),
      '#default_value' => $config->get('interaction_profile') ?: 'default',
      '#required' => TRUE,
    ];

    $form['include_content_accuracy'] = [
      '#type' => 'checkbox',
      '#title' => $this->t('Include content accuracy checks'),
      '#default_value' => (bool) $config->get('include_content_accuracy'),
    ];

    return parent::buildForm($form, $form_state);
  }

  /**
   * {@inheritdoc}
   */
  public function submitForm(array &$form, FormStateInterface $form_state): void {
    $settings = $this->configFactory()
      ->getEditable('remedy.settings')
      ->set('backend_base_url', rtrim((string) $form_state->getValue('backend_base_url'), '/'))
      ->set('site_code', strtoupper((string) $form_state->getValue('site_code')))
      ->set('interaction_profile', (string) $form_state->getValue('interaction_profile'))
      ->set('include_content_accuracy', (bool) $form_state->getValue('include_content_accuracy'));

    if ((bool) $form_state->getValue('api_token_clear')) {
      $settings->set('api_token', '');
    }
    elseif ((string) $form_state->getValue('api_token') !== '') {
      $settings->set('api_token', (string) $form_state->getValue('api_token'));
    }

    $settings->save();

    parent::submitForm($form, $form_state);
  }

}
