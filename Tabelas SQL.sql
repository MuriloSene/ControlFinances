-- (Re)criar o banco de dados
DROP DATABASE IF EXISTS control_finances;
CREATE DATABASE control_finances
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;
USE control_finances;

-- Usuários
CREATE TABLE usuarios (
  id INT AUTO_INCREMENT PRIMARY KEY,
  nome VARCHAR(100) NOT NULL,
  email VARCHAR(100) NOT NULL UNIQUE,
  senha VARCHAR(255) NOT NULL
);

-- Carteira (saldo atual)
CREATE TABLE carteira (
  id INT AUTO_INCREMENT PRIMARY KEY,
  usuario_id INT NOT NULL,
  saldo DECIMAL(10,2) DEFAULT 0.00,
  FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

-- Entradas financeiras
CREATE TABLE entradas (
  id INT AUTO_INCREMENT PRIMARY KEY,
  usuario_id INT NOT NULL,
  descricao TEXT,
  data DATETIME NOT NULL,
  instituicao VARCHAR(100) NOT NULL,
  valor DECIMAL(10,2) NOT NULL,
  FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

-- Saídas financeiras
CREATE TABLE saidas (
  id INT AUTO_INCREMENT PRIMARY KEY,
  usuario_id INT NOT NULL,
  descricao TEXT,
  data DATETIME NOT NULL,
  categoria VARCHAR(100) NOT NULL,
  subcategoria VARCHAR(100),   -- usado quando categoria = 'Outros'
  instituicao VARCHAR(255) NOT NULL,
  valor DECIMAL(10,2) NOT NULL,
  FOREIGN KEY (usuario_id) REFERENCES usuarios(id) ON DELETE CASCADE
);

show tables;