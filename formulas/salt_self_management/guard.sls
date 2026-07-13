{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set manage = guard.get('manage', true) %}
{% set enabled = guard.get('enabled', true) %}

{% if mode == 'installer' or not manage or not enabled %}
salt_self_management_guard_not_managed:
  test.nop:
    - name: Safe apply runtime is not managed in this execution mode
{% else %}
salt_self_management_guard_directory:
  file.directory:
    - name: /usr/local/libexec/salt-self-management
    - user: root
    - group: root
    - mode: '0755'
    - makedirs: true

salt_self_management_guard_helper:
  file.managed:
    - name: {{ guard.get('helper') | yaml_dquote }}
    - source: salt://formulas/salt_self_management/files/salt-safe-apply.py
    - user: root
    - group: root
    - mode: '0755'
    - require:
      - file: salt_self_management_guard_directory

salt_self_management_guard_config_directory:
  file.directory:
    - name: /etc/salt-self-management
    - user: root
    - group: root
    - mode: '0755'
    - makedirs: true

salt_self_management_guard_environment:
  file.managed:
    - name: {{ guard.get('environment_file') | yaml_dquote }}
    - source: salt://formulas/salt_self_management/files/guard.env.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0600'
    - require:
      - file: salt_self_management_guard_config_directory

salt_self_management_guard_unit:
  file.managed:
    - name: {{ guard.get('unit') | yaml_dquote }}
    - source: salt://formulas/salt_self_management/files/salt-self-management-apply@.service
    - user: root
    - group: root
    - mode: '0644'

salt_self_management_guard_daemon_reload:
  cmd.run:
    - name: systemctl daemon-reload
    - onchanges:
      - file: salt_self_management_guard_unit
{% endif %}
