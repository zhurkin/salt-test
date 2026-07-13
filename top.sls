base:
  '*':
    - formulas.salt_minion

  'G@roles:salt_master':
    - match: compound
    - formulas.salt_master
