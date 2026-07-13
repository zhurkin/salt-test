{% from "formulas/salt_master/map.jinja" import salt_master with context %}
{% set logrotate = salt_master.get('logrotate', {}) %}
{% set manage = logrotate.get('manage', false) %}
{% set enabled = logrotate.get('enabled', false) %}

{% if not manage %}
salt_master_logrotate_unmanaged:
  test.nop:
    - name: salt_master logrotate is externally managed
{% elif enabled %}
salt_master_logrotate:
  file.managed:
    - name: {{ salt_master.get('logrotate_config_file') | yaml_dquote }}
    - source: salt://formulas/salt_master/files/logrotate.conf.jinja
    - template: jinja
    - user: root
    - group: root
    - mode: '0644'
    - makedirs: true
{% else %}
salt_master_logrotate:
  file.absent:
    - name: {{ salt_master.get('logrotate_config_file') | yaml_dquote }}
{% endif %}
