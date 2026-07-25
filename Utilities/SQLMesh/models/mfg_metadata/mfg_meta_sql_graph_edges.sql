MODEL (
  name mfg_metadata.sql_graph_edges,
  kind SEED (
    path '$root/seeds/sql_graph_edges.csv'
  ),
  columns (
    _id TEXT,
    _key TEXT,
    _from TEXT,
    _to TEXT,
    edge_family TEXT,
    edge_type TEXT,
    perspective TEXT,
    unique_id TEXT
  ),
  grain (_id)
);
