{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set lock = salt_self_management.get('safe_apply', {}).get('installer_lock') %}
{% set locked = mode != 'installer' and lock and salt['file.file_exists'](lock) %}

{% if locked %}
salt_minion_installer_transaction_active:
  test.nop:
    - name: Salt installer transaction is active; salt_minion apply skipped
{% else %}
include:
  - formulas.salt_self_management.guard
  - formulas.salt_minion.config
  - formulas.salt_minion.schedule
  - formulas.salt_minion.logrotate
  - formulas.salt_minion.service
  - formulas.salt_minion.apply
{% endif %}
