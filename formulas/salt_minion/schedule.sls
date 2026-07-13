{% from "formulas/salt_minion/map.jinja" import salt_minion with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set schedule = salt_minion.get('schedule', {}).get('self_management', {}) %}
{% set manage = schedule.get('manage', false) %}
{% set enabled = schedule.get('enabled', false) %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}
{% if safe %}
{% set target = guard.get('candidate_dir') ~ '/minion/etc/salt/minion.d/30-schedule.conf' %}
{% else %}
{% set target = salt_minion.get('schedule_config_file') %}
{% endif %}

{% if not manage %}
salt_minion_self_management_schedule_unmanaged:
  test.nop:
    - name: salt_minion self-management schedule is externally managed
{% elif enabled %}
salt_minion_self_management_schedule:
  file.managed:
    - name: {{ target | yaml_dquote }}
    - source: salt://formulas/salt_minion/files/schedule.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
{% else %}
salt_minion_self_management_schedule:
  file.absent:
    - name: {{ target | yaml_dquote }}
{% endif %}
