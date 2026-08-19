-- Se ejecuta solo la primera vez que se crea el volumen de datos de Postgres.
-- Crea la base de datos de test, separada de la de desarrollo (spotify_clone),
-- usada por tests/test_auth.py y el resto de la suite de tests.
CREATE DATABASE spotify_clone_test;
