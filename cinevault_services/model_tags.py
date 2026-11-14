"""Index inferred tags separately from user-authored tags."""


def install_model_tag_index(db):
    db.execute('''CREATE TABLE IF NOT EXISTS model_tags(video_id TEXT NOT NULL,label TEXT NOT NULL,
        PRIMARY KEY(video_id,label))''')
    db.execute('CREATE INDEX IF NOT EXISTS model_tags_label ON model_tags(label,video_id)')
    db.execute('''CREATE VIEW IF NOT EXISTS video_model_tag_values AS
        SELECT v.id AS video_id,CASE WHEN j.type='object' THEN json_extract(j.value,'$.label') END AS label
        FROM videos v,json_each(v.data,'$.classification.details') j
        UNION ALL
        SELECT v.id AS video_id,j.value AS label FROM videos v,json_each(v.data,'$.ai_tags') j WHERE j.type='text' ''')
    # An outer UPSERT can take precedence over a trigger's OR IGNORE. Use
    # DISTINCT to deduplicate labels present in both inference outputs.
    values = "SELECT DISTINCT video_id,trim(label) FROM video_model_tag_values WHERE typeof(label)='text' AND length(trim(label)) BETWEEN 1 AND 100"
    db.execute(f'''CREATE TRIGGER IF NOT EXISTS model_tags_insert AFTER INSERT ON videos BEGIN
        INSERT OR IGNORE INTO model_tags {values} AND video_id=new.id; END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS model_tags_delete AFTER DELETE ON videos BEGIN
        DELETE FROM model_tags WHERE video_id=old.id; END''')
    db.execute(f'''CREATE TRIGGER IF NOT EXISTS model_tags_update AFTER UPDATE OF data ON videos
        WHEN json_extract(old.data,'$.classification.details') IS NOT json_extract(new.data,'$.classification.details')
          OR json_extract(old.data,'$.ai_tags') IS NOT json_extract(new.data,'$.ai_tags') BEGIN
        DELETE FROM model_tags WHERE video_id=old.id;
        INSERT OR IGNORE INTO model_tags {values} AND video_id=new.id; END''')
    if not db.execute("SELECT 1 FROM meta WHERE key='model_tags_v1'").fetchone():
        db.execute('INSERT OR IGNORE INTO model_tags '+values)
        db.execute("INSERT INTO meta VALUES('model_tags_v1',1)")
