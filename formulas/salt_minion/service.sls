{% from "formulas/salt_minion/map.jinja" import salt_minion with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set service = salt_minion.get('service', {}) %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}

{% if safe %}
salt_minion_service_guarded:
  test.nop:
    - name: salt-minion service state is reconciled by the protected activation helper
{% elif service.get('running', true) %}
salt_minion_service:
  service.running:
    - name: {{ service.get('name', 'salt-minion') | yaml_dquote }}
    - enable: {{ service.get('enabled', true) | yaml_encode }}
    - require:
      - file: salt_minion_master_config
{% if service.get('restart_on_change', false) %}
    - watch:
      - file: salt_minion_master_config
{% endif %}
{% else %}
salt_minion_service:
  service.dead:
    - name: {{ service.get('name', 'salt-minion') | yaml_dquote }}
    - enable: {{ service.get('enabled', false) | yaml_encode }}
{% endif %}
