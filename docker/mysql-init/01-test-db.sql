-- Banco usado pelo pytest
CREATE DATABASE IF NOT EXISTS meteo10_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON meteo10_test.* TO 'meteo10'@'%';
