-- Executar uma vez como root do MySQL local:  sudo mysql < setup_local_mysql.sql
CREATE DATABASE IF NOT EXISTS meteo10 CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS meteo10_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE USER IF NOT EXISTS 'meteo10'@'localhost' IDENTIFIED BY 'meteo10';
CREATE USER IF NOT EXISTS 'meteo10'@'127.0.0.1' IDENTIFIED BY 'meteo10';
GRANT ALL PRIVILEGES ON meteo10.* TO 'meteo10'@'localhost';
GRANT ALL PRIVILEGES ON meteo10_test.* TO 'meteo10'@'localhost';
GRANT ALL PRIVILEGES ON meteo10.* TO 'meteo10'@'127.0.0.1';
GRANT ALL PRIVILEGES ON meteo10_test.* TO 'meteo10'@'127.0.0.1';
FLUSH PRIVILEGES;
