DROP DATABASE IF EXISTS youtube;
CREATE DATABASE youtube
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;
USE youtube;

CREATE TABLE Users (
    user_id     INT UNSIGNED  NOT NULL PRIMARY KEY,
    username    VARCHAR(50)   NOT NULL UNIQUE,
    country     VARCHAR(60),
    join_date   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
    birthday    DATE,
    email       VARCHAR(256)  NOT NULL UNIQUE
);

CREATE TABLE Channels (
    channel_id      INT UNSIGNED    NOT NULL PRIMARY KEY,
    channel_name    VARCHAR(100)    NOT NULL,
    subscribe_cnt   BIGINT UNSIGNED NOT NULL DEFAULT 0,
    creation_date   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    total_view_cnt  BIGINT UNSIGNED NOT NULL DEFAULT 0,
    video_cnt       INT UNSIGNED    NOT NULL DEFAULT 0,
    country         VARCHAR(60),
    user_id         INT UNSIGNED    NOT NULL,
    CONSTRAINT fk_channels_user FOREIGN KEY (user_id)
        REFERENCES Users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Videos (
    video_id     INT UNSIGNED    NOT NULL PRIMARY KEY,
    video_name   VARCHAR(100)    NOT NULL,
    vid_length   INT UNSIGNED    NOT NULL CHECK (vid_length > 0),
    description  TEXT,
    thumbnail    VARCHAR(500),
    view_cnt     BIGINT UNSIGNED NOT NULL DEFAULT 0,
    upload_date  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    channel_id   INT UNSIGNED    NOT NULL,
    CONSTRAINT fk_videos_channel FOREIGN KEY (channel_id)
        REFERENCES Channels(channel_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Playlist (
    playlist_id    INT UNSIGNED NOT NULL PRIMARY KEY,
    creation_date  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    video_cnt      INT UNSIGNED NOT NULL DEFAULT 0,
    channel_id     INT UNSIGNED NOT NULL,
    CONSTRAINT fk_playlist_channel FOREIGN KEY (channel_id)
        REFERENCES Channels (channel_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Tags (
    tag_id    INT UNSIGNED NOT NULL PRIMARY KEY,
  	tag_name  VARCHAR(50)  NOT NULL UNIQUE
);

CREATE TABLE Comments (
    comment_id    INT UNSIGNED NOT NULL PRIMARY KEY,
    content       TEXT         NOT NULL,
    post_date     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    video_id      INT UNSIGNED NOT NULL,
    user_id       INT UNSIGNED NOT NULL,
    comment_parent  INT UNSIGNED NULL,
    CONSTRAINT fk_comments_video  FOREIGN KEY (video_id)
        REFERENCES Videos(video_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_comments_user   FOREIGN KEY (user_id)
        REFERENCES Users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_comments_parent FOREIGN KEY (comment_parent)
        REFERENCES Comments (comment_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Subscribe (
    user_id        INT UNSIGNED NOT NULL,
    channel_id     INT UNSIGNED NOT NULL,
    subscribed_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_subscribe         PRIMARY KEY (user_id, channel_id),
    CONSTRAINT fk_subscribe_user    FOREIGN KEY (user_id)
        REFERENCES Users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_subscribe_channel FOREIGN KEY (channel_id)
        REFERENCES Channels(channel_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Like_Dislike (
    user_id   INT UNSIGNED             NOT NULL,
    video_id  INT UNSIGNED             NOT NULL,
    type      ENUM('like', 'dislike')  NOT NULL,
    rated_at  DATETIME                 NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_like_dislike       PRIMARY KEY (user_id, video_id),
    CONSTRAINT fk_like_dislike_user  FOREIGN KEY (user_id)
        REFERENCES Users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_like_dislike_video FOREIGN KEY (video_id)
        REFERENCES Videos(video_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

-- apparently View is a reserved word in sql so whenever you want to access it always backquote it
CREATE TABLE `View` (
    user_id    INT UNSIGNED NOT NULL,
    video_id   INT UNSIGNED NOT NULL,
    viewed_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_view       PRIMARY KEY (user_id, video_id),
    CONSTRAINT fk_view_user  FOREIGN KEY (user_id)
        REFERENCES Users(user_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_view_video FOREIGN KEY (video_id)
        REFERENCES Videos(video_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Contain (
    video_id     INT UNSIGNED NOT NULL,
    playlist_id  INT UNSIGNED NOT NULL,
    CONSTRAINT pk_contain          PRIMARY KEY (video_id, playlist_id),
    CONSTRAINT fk_contain_video    FOREIGN KEY (video_id)
        REFERENCES Videos (video_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_contain_playlist FOREIGN KEY (playlist_id)
        REFERENCES Playlist (playlist_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE Tagged (
    video_id  INT UNSIGNED NOT NULL,
    tag_id    INT UNSIGNED NOT NULL,
    CONSTRAINT pk_tagged       PRIMARY KEY (video_id, tag_id),
    CONSTRAINT fk_tagged_video FOREIGN KEY (video_id)
        REFERENCES Videos(video_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT fk_tagged_tag   FOREIGN KEY (tag_id)
        REFERENCES Tags(tag_id)
        ON DELETE CASCADE ON UPDATE CASCADE
);