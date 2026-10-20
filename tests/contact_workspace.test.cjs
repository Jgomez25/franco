const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const context = vm.createContext({localStorage:{getItem:()=>'{bad json'}});
vm.runInContext(fs.readFileSync('franco/web/contacts.js','utf8'), context);
test('CSV neutralizes formulas and preserves quoting and line breaks', () => {
  for (const value of ['=1+1',' +SUM(A1)','@cmd','-5','\tformula']) {
    assert.ok(context.contactCsvCell(value).startsWith('"\''));
  }
  assert.equal(context.contactCsvCell('a,"b"\nc'), '"a,""b""\nc"');
  assert.equal(context.contactCsvCell(null), '""');
});
test('Corrupt saved views do not crash the workspace', () => {
  assert.equal(context.readContactViews().length,0);
});
