-- Extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- Finnish alphabetical collation (ICU provider)
CREATE COLLATION IF NOT EXISTS fi_icu (provider = icu, locale = 'fi-FI');

-- Case- and diacritic-insensitive nondeterministic collation
CREATE COLLATION IF NOT EXISTS case_insensitive (
    provider = icu,
    locale = 'und-u-ks-level2',
    deterministic = false
);

