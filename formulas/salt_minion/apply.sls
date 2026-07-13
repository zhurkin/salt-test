{% from "formulas/salt_minion/map.jinja" import salt_minion with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set helper = guard.get('helper') %}
{% set python = guard.get('python', '/opt/saltstack/salt/bin/python3') %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}
{% set schedule = salt_minion.get('schedule', {}).get('self_management', {}) %}

{% if safe %}
salt_minion_candidate_manifest:
  file.managed:
    - name: {{ (guard.get('candidate_dir') ~ '/minion/manifest.json') | yaml_dquote }}
    - source: salt://formulas/salt_minion/files/candidate-manifest.json.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0600'
    - makedirs: true
    - require:
      - file: salt_minion_master_config
{% if schedule.get('manage', false) %}
      - file: salt_minion_self_management_schedule
{% endif %}

salt_minion_safe_candidate_queue:
  cmd.run:
    - name: {{ (python ~ ' ' ~ helper ~ ' queue minion') | yaml_dquote }}
    - stateful: true
    - require:
      - file: salt_self_management_guard_helper
      - file: salt_self_management_guard_environment
      - file: salt_self_management_guard_unit
      - cmd: salt_self_management_guard_daemon_reload
      - file: salt_minion_candidate_manifest
{% else %}
salt_minion_safe_candidate_queue_disabled:
  test.nop:
    - name: salt_minion safe candidate activation is disabled in installer/direct mode
{% endif %}
