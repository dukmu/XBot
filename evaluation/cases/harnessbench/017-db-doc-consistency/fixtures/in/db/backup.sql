-- MySQL dump 10.13  Distrib 8.0.32, for Linux (x86_64)
-- Host: localhost    Database: prod_env

DROP TABLE IF EXISTS `system_config`;
CREATE TABLE `system_config` (
  `id` int NOT NULL AUTO_INCREMENT,
  `config_key` varchar(255) NOT NULL,
  `config_value` varchar(255) NOT NULL,
  `environment` varchar(64) DEFAULT 'global',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

LOCK TABLES `system_config` WRITE;
INSERT INTO `system_config` VALUES 
(1,'max_db_connections','500','global'),
(2,'cache_ttl_seconds','600','global'),
(3,'api_rate_limit','100','global'),
(4,'default_theme','dark','global'),
(5,'worker_timeout','30','global'),
(6,'db_connection_pool_size','150','global'),
(7,'enable_ssl','true','global'),
(8,'log_level','INFO','global'),
(9,'max_upload_size_mb','50','global'),
(10,'session_timeout_min','15','global'),
(11,'api_rate_limit','800','staging'),
(12,'worker_timeout','120','staging'),
(13,'log_level','DEBUG','staging'),
(14,'max_upload_size_mb','100','staging');
UNLOCK TABLES;

DROP TABLE IF EXISTS `cluster_nodes`;
CREATE TABLE `cluster_nodes` (
  `id` int NOT NULL AUTO_INCREMENT,
  `node_id` varchar(64) NOT NULL,
  `role` varchar(32) NOT NULL,
  `cpu_cores` int NOT NULL,
  `memory_gb` int NOT NULL,
  `disk_gb` int NOT NULL,
  `region` varchar(32) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

LOCK TABLES `cluster_nodes` WRITE;
INSERT INTO `cluster_nodes` VALUES 
(1,'web-01','web',8,16,200,'us-east-1'),
(2,'web-02','web',8,16,200,'us-east-1'),
(3,'api-01','api',16,32,500,'us-east-1'),
(4,'api-02','api',16,32,500,'us-east-1'),
(5,'db-master','database',32,64,2000,'us-east-1'),
(6,'db-replica-01','database',16,32,1000,'us-west-2'),
(7,'cache-01','cache',8,64,100,'us-east-1'),
(8,'worker-01','worker',16,64,500,'us-west-2');
UNLOCK TABLES;