-- Runs once, when the postgres image first initialises an empty data volume.
-- The tests service points at this database; see docker-compose.yml.
CREATE DATABASE idp_pipeline_test OWNER idp;
