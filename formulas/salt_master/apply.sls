{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set helper = guard.get('helper') %}
{% set python = guard.get('python', '/opt/saltstack/salt/bin/python3') %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}

{% if safe %}
salt_master_candidate_manifest:
  file.managed:
    - name: {{ (guard.get('candidate_dir') ~ '/master/manifest.json') | yaml_dquote }}
    - source: salt://formulas/salt_master/files/candidate-manifest.json.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - require:
      - file: salt_master_network_config
      - file: salt_master_auto_accept_config
      - file: salt_master_project_roots_config
      - cmd: salt_master_reactor_registry_config

salt_master_safe_candidate_queue:
  cmd.run:
    - name: {{ (python ~ ' ' ~ helper ~ ' queue master') | yaml_dquote }}
    - stateful: true
    - require:
      - file: salt_self_management_guard_helper
      - file: salt_self_management_guard_environment
      - file: salt_self_management_guard_unit
      - cmd: salt_self_management_guard_daemon_reload
      - file: salt_master_candidate_manifest
{% else %}
salt_master_safe_candidate_queue_disabled:
  test.nop:
    - name: salt_master safe candidate activation is disabled in installer/direct mode
{% endif %}
