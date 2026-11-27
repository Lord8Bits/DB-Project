USE yt_videos;

CREATE TABLE IF NOT EXISTS videos (
    video_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    category_id INT NOT NULL,
    video_name VARCHAR(255) NOT NULL,
    description TEXT,
    vid_length INT UNSIGNED NOT NULL,
    thumbnail VARCHAR(2048),
    view_cnt BIGINT NOT NULL DEFAULT 0,
    like_cnt BIGINT NOT NULL DEFAULT 0,
    upload_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (video_id),
    KEY idx_videos_channel_id (channel_id),
    KEY idx_videos_category_id (category_id),
    FULLTEXT KEY ft_videos_search (video_name, description)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS video_tag (
    video_id BIGINT NOT NULL,
    tag_id BIGINT NOT NULL,
    PRIMARY KEY (video_id, tag_id),
    CONSTRAINT fk_videotag_video FOREIGN KEY (video_id) REFERENCES videos(video_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS channel_video_lookup (
    channel_id BIGINT NOT NULL,
    video_id BIGINT NOT NULL,
    keyspace_id VARBINARY(128) NOT NULL,
    PRIMARY KEY (channel_id, video_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS categories (
    category_id INT NOT NULL,
    category_name VARCHAR(100) NOT NULL,
    PRIMARY KEY (category_id)
) ENGINE=InnoDB;