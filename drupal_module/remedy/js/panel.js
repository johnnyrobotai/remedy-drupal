((Drupal, drupalSettings, once) => {
  async function fetchJson(url, options = {}) {
    const response = await fetch(url, {
      headers: {'Accept': 'application/json'},
      credentials: 'same-origin',
      ...options
    });
    if (!response.ok) {
      throw new Error(`Request failed: ${response.status}`);
    }
    return response.json();
  }

  function renderSummary(container, payload) {
    if (!container) {
      return;
    }
    const summary = payload.summary || {};
    const total = summary.total_issues || 0;
    const impacts = summary.by_impact || {};

    const card = document.createElement('div');
    card.className = 'remedy-summary-card';

    const totalEl = document.createElement('strong');
    totalEl.textContent = String(total);
    card.append(totalEl, ' open issue(s)');

    const meta = document.createElement('div');
    meta.className = 'remedy-summary-card__meta';
    meta.textContent = [
      `Critical: ${impacts.critical || 0}`,
      `Serious: ${impacts.serious || 0}`,
      `Moderate: ${impacts.moderate || 0}`,
      `Minor: ${impacts.minor || 0}`
    ].join(' ');
    card.append(meta);

    container.replaceChildren(card);
  }

  function renderIssues(container, payload) {
    if (!container) {
      return;
    }
    const issues = payload.issues || [];
    if (!issues.length) {
      const empty = document.createElement('p');
      empty.className = 'remedy-empty';
      empty.textContent = 'No issues found.';
      container.replaceChildren(empty);
      return;
    }

    const cards = issues.map((issue) => {
      const impact = String(issue.impact || 'unknown');
      const impactClass = impact.toLowerCase().replace(/[^a-z0-9_-]/g, '') || 'unknown';
      const button = document.createElement('button');
      button.className = 'remedy-issue-card';
      button.type = 'button';
      button.dataset.selector = (issue.primary_target && issue.primary_target.selector) || '';

      const impactEl = document.createElement('span');
      impactEl.className = `remedy-issue-card__impact remedy-issue-card__impact--${impactClass}`;
      impactEl.textContent = impact;

      const rule = document.createElement('span');
      rule.className = 'remedy-issue-card__rule';
      rule.textContent = String(issue.rule_id || '');

      const description = document.createElement('span');
      description.className = 'remedy-issue-card__desc';
      description.textContent = String(issue.description || '');

      button.append(impactEl, rule, description);
      return button;
    });
    container.replaceChildren(...cards);

    cards.forEach((button) => {
      button.addEventListener('click', () => {
        const selector = button.getAttribute('data-selector');
        if (window.RemedyHighlight) {
          window.RemedyHighlight.highlight(selector);
        }
      });
    });
  }

  async function loadPanel(config, root) {
    const summary = root.querySelector('[data-remedy-summary]');
    const issues = root.querySelector('[data-remedy-issues]');
    try {
      const payload = await fetchJson(config.issuesUrl);
      renderSummary(summary, payload);
      renderIssues(issues, payload);
    }
    catch (error) {
      if (issues) {
        const message = document.createElement('p');
        message.className = 'remedy-empty';
        message.textContent = 'Unable to load issues.';
        issues.replaceChildren(message);
      }
    }
  }

  Drupal.behaviors.remedyPanel = {
    attach(context) {
      const config = drupalSettings.remedy && drupalSettings.remedy.panel;
      if (!config) {
        return;
      }

      once('remedy-panel', '.remedy-panel, .remedy-page-report', context).forEach((root) => {
        loadPanel(config, root);
      });
    }
  };
})(Drupal, drupalSettings, once);
