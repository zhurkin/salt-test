{% from "formulas/salt_master/map.jinja" import salt_master with context %}
{% from "formulas/salt_self_management/map.jinja" import salt_self_management with context %}
{% set mode = salt_self_management.get('execution_mode', 'safe') %}
{% set guard = salt_self_management.get('safe_apply', {}) %}
{% set safe = mode != 'installer' and guard.get('manage', true) and guard.get('enabled', true) %}
{% set python = guard.get('python', '/opt/saltstack/salt/bin/python3') %}
{% set compiler_dir = '/usr/local/libexec/salt-master' %}
{% set compiler = compiler_dir ~ '/reactor-registry.py' %}
{% set fileserver = salt_master.get('fileserver', {}) %}
{% set fileserver_mode = fileserver.get('mode', 'roots') %}
{% set project_root = salt_master.get('project_root', '/srv/salt') %}
{# During a roots-to-GitFS installer transaction, the running master still has
   only roots enabled. Compile from the configured local project root;
   protected post-restart health then verifies required files in GitFS.
   Normal highstates after activation compile through the active GitFS backend. #}
{% set compile_from_gitfs = fileserver_mode == 'gitfs' and mode != 'installer' %}
{% set compiler_source = 'fileserver' if compile_from_gitfs else 'local' %}
{% set backend_arg = ' --backend gitfs' if compile_from_gitfs else '' %}
{% if safe %}
{% set output = guard.get('candidate_dir') ~ '/master/etc/salt/master.d/40-reactor.conf' %}
{% else %}
{% set output = '/etc/salt/master.d/40-reactor.conf' %}
{% endif %}

salt_master_reactor_compiler_directory:
  file.directory:
    - name: {{ compiler_dir | yaml_dquote }}
    - user: root
    - group: root
    - mode: '0755'

salt_master_reactor_compiler:
  file.managed:
    - name: {{ compiler | yaml_dquote }}
    - source: salt://formulas/salt_master/files/reactor-registry.py
    - user: root
    - group: root
    - mode: '0755'
    - require:
      - file: salt_master_reactor_compiler_directory

salt_master_reactor_registry_config:
  cmd.run:
    - name: {{ (python ~ ' ' ~ compiler ~ ' compile --source ' ~ compiler_source ~ ' --root ' ~ project_root ~ ' --registry-prefix reactor --output ' ~ output ~ backend_arg ~ ' --salt-run /opt/saltstack/salt/salt-run --salt-call /opt/saltstack/salt/salt-call') | yaml_dquote }}
    - stateful: true
    - require:
      - file: salt_master_reactor_compiler
