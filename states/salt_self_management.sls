{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set roles = salt['grains.get']('roles', []) %}
{% set lock = salt_self_management.get('safe_apply', {}).get(
    'installer_lock', '/run/salt-installer/self-management.lock') %}

{% if salt['file.file_exists'](lock) %}
salt_self_management_installer_transaction_active:
  test.nop:
    - name: Salt installer transaction is active; scheduled self-management skipped
{% else %}
include:
  - formulas.salt_minion
{% if 'salt_master' in roles %}
  - formulas.salt_master
{% endif %}
{% endif %}
