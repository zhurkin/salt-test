{% from "formulas/salt_minion/map.jinja" import salt_minion with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}
{% if safe %}
{% set target = guard.get('candidate_dir') ~ '/minion/etc/salt/minion.d/10-master.conf' %}
{% else %}
{% set target = salt_minion.get('master_config_file') %}
{% endif %}

salt_minion_master_config:
  file.managed:
    - name: {{ target | yaml_dquote }}
    - source: salt://formulas/salt_minion/files/master.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
