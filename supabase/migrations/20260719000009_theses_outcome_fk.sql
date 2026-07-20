-- Deferred FK: theses.outcome_trade_id -> trades(id).
--
-- Added here rather than in 20260719000005_theses.sql because trades does not exist
-- until 20260719000008_trades.sql -- theses -> trades -> orders -> decisions -> theses
-- would otherwise be a circular dependency across CREATE TABLE statements within a
-- single migration file. Column and index already exist on theses; this migration only
-- adds the constraint once its target table is available.

alter table theses
    add constraint theses_outcome_trade_id_fkey
    foreign key (outcome_trade_id) references trades (id);

create index theses_outcome_trade_id_idx on theses (outcome_trade_id);
