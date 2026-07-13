{% from "formulas/salt_master/map.jinja" import salt_master with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}
{% if safe %}
{% set root = guard.get('candidate_dir') ~ '/master/etc/salt/master.d' %}
{% else %}
{% set root = '/etc/salt/master.d' %}
{% endif %}

salt_master_network_config:
  file.managed:
    - name: {{ (root ~ '/10-network.conf') | yaml_dquote }}
    - source: salt://formulas/salt_master/files/network.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true

salt_master_auto_accept_config:
  file.managed:
    - name: {{ (root ~ '/20-auto-accept.conf') | yaml_dquote }}
    - source: salt://formulas/salt_master/files/auto-accept.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true

salt_master_project_roots_config:
  file.managed:
    - name: {{ (root ~ '/30-project-roots.conf') | yaml_dquote }}
    - source: salt://formulas/salt_master/files/project-roots.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
