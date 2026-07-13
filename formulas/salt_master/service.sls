{% from "formulas/salt_master/map.jinja" import salt_master with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set service = salt_master.get('service', {}) %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}

{% if safe %}
salt_master_service_guarded:
  test.nop:
    - name: salt-master service state is reconciled by the protected activation helper
{% elif service.get('running', true) %}
salt_master_service:
  service.running:
    - name: {{ service.get('name', 'salt-master') | yaml_dquote }}
    - enable: {{ service.get('enabled', true) | yaml_encode }}
    - require:
      - file: salt_master_network_config
      - file: salt_master_auto_accept_config
      - file: salt_master_project_roots_config
      - cmd: salt_master_reactor_registry_config
{% if service.get('restart_on_change', false) %}
    - watch:
      - file: salt_master_network_config
      - file: salt_master_auto_accept_config
      - file: salt_master_project_roots_config
      - cmd: salt_master_reactor_registry_config
{% endif %}
{% else %}
salt_master_service:
  service.dead:
    - name: {{ service.get('name', 'salt-master') | yaml_dquote }}
    - enable: {{ service.get('enabled', false) | yaml_encode }}
{% endif %}
