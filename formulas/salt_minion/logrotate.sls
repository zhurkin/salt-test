{% from "formulas/salt_minion/map.jinja" import salt_minion with context %}
{% set logrotate = salt_minion.get('logrotate', {}) %}
{% set manage = logrotate.get('manage', false) %}
{% set enabled = logrotate.get('enabled', false) %}

{% if not manage %}
salt_minion_logrotate_unmanaged:
  test.nop:
    - name: salt_minion logrotate is externally managed
{% elif enabled %}
salt_minion_logrotate:
  file.managed:
    - name: {{ salt_minion.get('logrotate_config_file') | yaml_dquote }}
    - source: salt://formulas/salt_minion/files/logrotate.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
{% else %}
salt_minion_logrotate:
  file.absent:
    - name: {{ salt_minion.get('logrotate_config_file') | yaml_dquote }}
{% endif %}
