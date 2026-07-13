{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set lock = salt_self_management.get('safe_apply', {}).get('installer_lock') %}
{% set locked = mode != 'installer' and lock and salt['file.file_exists'](lock) %}

{% if locked %}
salt_master_installer_transaction_active:
  test.nop:
    - name: Salt installer transaction is active; salt_master apply skipped
{% else %}
include:
  - formulas.salt_self_management.guard
  - formulas.salt_master.config
  - formulas.salt_master.reactor
  - formulas.salt_master.logrotate
  - formulas.salt_master.service
  - formulas.salt_master.apply
{% endif %}
