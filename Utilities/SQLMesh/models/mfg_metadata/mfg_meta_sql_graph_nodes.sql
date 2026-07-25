MODEL (
  name mfg_metadata.sql_graph_nodes,
  kind SEED (
    path '$root/seeds/sql_graph_nodes.csv'
  ),
  columns (
    _id TEXT,
    _key TEXT,
    node_type TEXT,
    node_family TEXT,
    perspective TEXT,
    table_name TEXT,
    column_slot TEXT,
    predicate TEXT,
    unique_id TEXT,
    description TEXT
  ),
  grain (_id)
);
