USE yt_core;

CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT NOT NULL,
    username VARCHAR(50) NOT NULL,
    email VARCHAR(254) NOT NULL,
    country CHAR(2) NOT NULL,
    join_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    birthday DATE NULL,
    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_username (username),
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS channels (
    channel_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    channel_name VARCHAR(100) NOT NULL,
    subscribe_cnt BIGINT NOT NULL DEFAULT 0,
    total_view_cnt BIGINT NOT NULL DEFAULT 0,
    video_cnt INT UNSIGNED NOT NULL DEFAULT 0,
    country CHAR(2) NOT NULL,
    creation_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (channel_id),
    KEY idx_channels_user_id (user_id),
    CONSTRAINT fk_channels_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS categories (
    category_id INT NOT NULL,
    category_name VARCHAR(100) NOT NULL,
    PRIMARY KEY (category_id),
    UNIQUE KEY uq_categories_name (category_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS tags (
    tag_id BIGINT NOT NULL,
    tag_name VARCHAR(100) NOT NULL,
    PRIMARY KEY (tag_id),
    UNIQUE KEY uq_tags_name (tag_name)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    subscribed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, channel_id),
    CONSTRAINT fk_sub_user FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    CONSTRAINT fk_sub_channel FOREIGN KEY (channel_id) REFERENCES channels(channel_id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- Global sequence replacing AUTO_INCREMENT across shards
CREATE TABLE IF NOT EXISTS video_seq (
    id INT NOT NULL,
    next_id BIGINT NOT NULL,
    cache BIGINT NOT NULL,
    PRIMARY KEY (id)
) ENGINE=InnoDB COMMENT='vitess_sequence';

INSERT IGNORE INTO video_seq (id, next_id, cache) VALUES (0, 1000, 100);