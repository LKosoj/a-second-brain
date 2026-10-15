"""Exercise the optional QMD SQLite backend without models or runtime data."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_scope_filters_vectors_and_text_before_top_results():
    roots = [
        Path.home() / ".local/lib/node_modules/@tobilu/qmd",
        Path("/usr/local/lib/node_modules/@tobilu/qmd"),
    ]
    root = next((path for path in roots if (path / "dist/store.js").exists()), None)
    if root is None or shutil.which("node") is None:
        pytest.skip("optional QMD Node package is not installed")
    script = Path(__file__).parents[1] / "src/d_brain/scripts/qmd_query_vec.mjs"
    source = """
      const {createStore} = await import(process.argv[1]);
      const {searchScoped} = await import(process.argv[2]);
      const store = createStore(':memory:');
      try {
        store.ensureVecTable(2);
        const docs = [['wanted', 'lessons/one.md', [0.6, 0.8]],
                      ['second', 'lessons/two.md', [0.5, 0.866]],
                      ['sibling', 'lessons-other/one.md', [1, 0]]];
        for (let i = 0; i < 40; i++) docs.push(['other'+i, 'other/'+i+'.md', [1, 0]]);
        for (const [hash, path, vector] of docs) {
          store.db.prepare('INSERT INTO content(hash,doc,created_at) VALUES (?,?,?)')
            .run(hash, 'report '.repeat(hash.startsWith('other') ? 10 : 1),
              '2026-10-15');
          store.db.prepare(`INSERT INTO documents
            (collection,path,title,hash,created_at,modified_at) VALUES (?,?,?,?,?,?)`)
            .run('thoughts',path,path,hash,'2026-10-15','2026-10-15');
          store.db.prepare(`INSERT INTO content_vectors
            (hash,seq,pos,model,embedded_at) VALUES (?,0,0,?,?)`)
            .run(hash,'test','2026-10-15');
          store.db.prepare('INSERT INTO vectors_vec(hash_seq,embedding) VALUES (?,?)')
            .run(hash+'_0',new Float32Array(vector));
        }
        const results = {};
        for (const [name, scope] of Object.entries({
          directory: ['qmd://thoughts/lessons'],
          files: ['qmd://thoughts/lessons/one.md', 'qmd://thoughts/lessons/two.md'],
          missing: ['qmd://thoughts/missing'],
        })) {
          results[name] = searchScoped(store,'report',[1,0],scope,2)
            .map(hits => hits.map(hit => hit.filepath).sort());
        }
        console.log(JSON.stringify(results));
      } finally {store.close();}
    """
    result = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            source,
            (root / "dist/store.js").as_uri(),
            script.as_uri(),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    payload = json.loads(result.stdout)
    expected = ["qmd://thoughts/lessons/one.md", "qmd://thoughts/lessons/two.md"]
    assert payload["directory"] == [expected, expected]
    assert payload["files"] == [expected, expected]
    assert payload["missing"] == [[], []]
