-- WNT post-cold-open LIVE-money tables, v1.
-- Additive only: does not touch nolive_runs / nolive_orders / nolive_markets / nolive_fills / nolive_trades /
-- nolive_depth / nolive_state / nolive_activity, and does not touch any nofade_* or gap_* table.
-- Safe to run twice. The app also runs this file at start-up (same as 001).

-- one row per night for the LIVE engine (separate from nolive_runs, which is the paper 5-cancel-time engine)
create table if not exists nolive_live_runs (
  id              bigserial primary key,
  event_date      text not null,
  event_ticker    text not null default '',
  status          text not null default 'fired',
  mode            text not null default 'dry_run',
  detected_at     timestamptz,
  markets_seen    int not null default 0,
  orders_placed   int not null default 0,
  orders_rejected int not null default 0,
  collateral      double precision not null default 0,
  cancelled_at    timestamptz,
  cancel_verified boolean,
  notes           text,
  created_at      timestamptz not null default now(),
  unique (event_date)
);

-- one row per word per night: the REAL (or dry-run-simulated) order at the single live cancel time
create table if not exists nolive_live_orders (
  id                    bigserial primary key,
  client_order_id       text not null,
  event_date            text not null,
  event_ticker          text not null default '',
  market_ticker         text not null,
  word                  text,
  no_price_cents        int not null,
  yes_price_cents       int not null,
  contracts             double precision not null,
  dollars               double precision not null,
  collateral            double precision not null,
  placed_at             timestamptz not null,
  order_id              text,
  mode                  text not null default 'dry_run',   -- dry_run | smoke | live
  dry_run               boolean not null default true,
  post_only             boolean not null default true,
  took_at_open          boolean not null default false,
  expiration_epoch      bigint,
  status                text not null default 'resting',    -- resting | filled | cancelled | rejected | dry_run
  reject_reason         text,
  filled_contracts      double precision not null default 0,
  first_fill_at         timestamptz,
  avg_fill_price_cents  double precision,
  fees_cents            double precision not null default 0,
  cancelled_at          timestamptz,
  result                text,
  realized_pnl_cents    double precision,
  created_at            timestamptz not null default now(),
  unique (client_order_id)
);
create index if not exists nolive_live_orders_date_idx on nolive_live_orders (event_date);
create index if not exists nolive_live_orders_mode_idx on nolive_live_orders (mode);

-- every real fill on a live/smoke order (dry_run orders have no fills -- see nolive_orders/c1755 for that proxy)
create table if not exists nolive_live_fills (
  id            bigserial primary key,
  fill_id       text not null,
  order_id      text,
  event_date    text not null,
  market_ticker text not null,
  contracts     double precision not null,
  price_cents   double precision not null,
  is_taker      boolean not null default false,
  fee_cents     double precision not null default 0,
  created_at    timestamptz not null,
  raw           text,
  unique (fill_id)
);
create index if not exists nolive_live_fills_date_idx on nolive_live_fills (event_date, market_ticker);
