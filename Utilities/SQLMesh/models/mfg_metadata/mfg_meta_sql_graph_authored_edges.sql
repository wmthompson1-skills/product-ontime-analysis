MODEL (
  name mfg_metadata.sql_graph_authored_edges,
  kind SEED (
    path '$root/seeds/sql_graph_authored_edges.csv'
  ),
  columns (
    authored_id INTEGER,
    edge_type TEXT,
    from_table TEXT,
    from_column TEXT,
    to_table TEXT,
    to_column TEXT,
    perspective TEXT,
    weight DOUBLE,
    concept TEXT,
    created_by TEXT,
    created_at TIMESTAMP
  ),
  grain (authored_id)
);
