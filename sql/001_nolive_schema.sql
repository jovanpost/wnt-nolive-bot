-- WNT post-cold-open PAPER bot (nolive) schema, v1.0
-- Run ONCE in the SAME Supabase project as wnt-nofade and wnt-gap-bot.
-- Every table is prefixed nolive_ so nothing collides with days / orders / fills / depth / gap_*.
-- Safe to run twice (create ... if not exists). The app also runs this file at start-up.

create table if not exists nolive_state (
  key         text primary key,
  value       text,
  updated_at  timestamptz not null default now()
);

create table if not exists nolive_activity (
  id          bigserial primary key,
  ts          timestamptz not null default now(),
  kind        text not null,
  message     text not null
);
create index if not exists nolive_activity_ts_idx on nolive_activity (ts);

-- one row per night
create table if not exists nolive_runs (
  id              bigserial primary key,
  event_date      text not null,
  event_ticker    text not null default '',
  status          text not null default 'fired',
  scheduled_at    timestamptz,
  fired_at        timestamptz,
  late_seconds    double precision,
  limit_yes_cents int not null,
  dollars         double precision not null,
  skip_at_or_above double precision not null,
  cancel_times    text,
  markets_seen    int not null default 0,
  qualified       int not null default 0,
  skipped         int not null default 0,
  fee_type        text,
  fee_multiplier  double precision,
  maker_rate      double precision,
  version         text,
  notes           text,
  settled_at      timestamptz,
  created_at      timestamptz not null default now(),
  unique (event_date)
);

-- one row per word per night: what the price and book looked like at fire time
create table if not exists nolive_markets (
  id                 bigserial primary key,
  run_id             bigint not null references nolive_runs(id) on delete cascade,
  event_date         text not null,
  market_ticker      text not null,
  word               text,
  title              text,
  is_counting        boolean not null default false,
  count_needed       int,
  market_status      text,
  last_price_cents   double precision,
  yes_bid_cents      double precision,
  yes_ask_cents      double precision,
  price_basis        text,
  yes_price_cents    double precision,
  qualified          boolean not null default false,
  skip_reason        text,
  book_yes           text,
  book_no            text,
  yes_size_at_limit  double precision,
  queue_ahead        double precision,
  pre_ts             timestamptz,
  pre_yes_bid_cents  double precision,
  pre_no_bid_cents   double precision,
  result             text,
  result_source      text,
  result_at          timestamptz,
  unique (run_id, market_ticker)
);
create index if not exists nolive_markets_date_idx on nolive_markets (event_date);

-- one row per word per night per cancel time (the "versions")
create table if not exists nolive_orders (
  id                 bigserial primary key,
  run_id             bigint not null references nolive_runs(id) on delete cascade,
  event_date         text not null,
  event_ticker       text not null default '',
  market_ticker      text not null,
  word               text,
  variant_id         text not null,
  cancel_ct          text not null,
  cancel_at          timestamptz not null,
  limit_yes_cents    int not null,
  contracts          double precision not null,
  dollars            double precision not null,
  placed_at          timestamptz not null,
  yes_price_at_place double precision,
  is_counting        boolean not null default false,
  status             text not null default 'resting',
  taker_contracts    double precision not null default 0,
  maker_contracts    double precision not null default 0,
  filled_contracts   double precision not null default 0,
  taker_fee_cents    double precision not null default 0,
  maker_fee_cents    double precision not null default 0,
  risk_cents         double precision not null default 0,
  proceeds_cents     double precision not null default 0,
  result             text,
  taker_pnl_cents    double precision,
  maker_pnl_cents    double precision,
  pnl_cents          double precision,
  settled_at         timestamptz,
  created_at         timestamptz not null default now(),
  unique (event_date, market_ticker, variant_id)
);
create index if not exists nolive_orders_date_idx on nolive_orders (event_date);
create index if not exists nolive_orders_variant_idx on nolive_orders (variant_id);

-- every simulated fill (shared by the versions: a version counts the fills up to its cancel time)
create table if not exists nolive_fills (
  id              bigserial primary key,
  run_id          bigint not null references nolive_runs(id) on delete cascade,
  event_date      text not null,
  market_ticker   text not null,
  ts              timestamptz not null,
  kind            text not null,
  contracts       double precision not null,
  price_cents     double precision not null,
  fee_cents       double precision not null default 0,
  source          text not null,
  ref             text not null,
  unique (event_date, market_ticker, ref)
);
create index if not exists nolive_fills_date_idx on nolive_fills (event_date, market_ticker);

-- raw trades seen after fire time (so any other rule can be replayed later)
create table if not exists nolive_trades (
  trade_id          text primary key,
  event_date        text not null,
  market_ticker     text not null,
  ts                timestamptz not null,
  yes_price_cents   double precision not null,
  contracts         double precision not null,
  taker_side        text
);
create index if not exists nolive_trades_idx on nolive_trades (event_date, market_ticker, ts);

-- order-book pictures after 5:29 PM (nofade stops at 5:28)
create table if not exists nolive_depth (
  id                   bigserial primary key,
  run_id               bigint references nolive_runs(id) on delete cascade,
  event_date           text not null,
  market_ticker        text not null,
  ts                   timestamptz not null,
  kind                 text not null default 'poll',
  best_yes_bid         double precision,
  best_no_bid          double precision,
  yes_size_total       double precision,
  no_size_total        double precision,
  yes_size_at_limit    double precision,
  book_yes             text,
  book_no              text
);
create index if not exists nolive_depth_idx on nolive_depth (event_date, market_ticker, ts);
