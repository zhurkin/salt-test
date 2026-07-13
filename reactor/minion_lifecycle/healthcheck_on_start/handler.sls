{# Safe starter Reactor: query only the minion that emitted the start event. #}
{% set minion_id = data.get('id', '') %}
{% if minion_id %}
minion_lifecycle_healthcheck_on_start:
  local.test.version:
    - tgt: {{ minion_id | yaml_dquote }}
{% endif %}
