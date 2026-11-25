import time
from cassandra.cluster import Cluster
from cassandra.query import dict_factory
from .config import settings

K = settings.cassandra_keyspace
SCHEMA = [
    "CREATE KEYSPACE IF NOT EXISTS " + K +
    " WITH replication = {'class':'SimpleStrategy','replication_factor':1}",
    "CREATE TABLE IF NOT EXISTS " + K + ".comments_by_video (" +
    "video_id bigint, month text, shard int, comment_id timeuuid, " +
    "user_id bigint, author_name text, body text, created_at timestamp, " +
    "PRIMARY KEY ((video_id, month, shard), comment_id)) " +
    "WITH CLUSTERING ORDER BY (comment_id DESC)",
    "CREATE TABLE IF NOT EXISTS " + K + ".comment_locations (" +
    "comment_id timeuuid PRIMARY KEY, video_id bigint, month text, shard int)",
    "CREATE TABLE IF NOT EXISTS " + K + ".replies_by_comment (" +
    "comment_id timeuuid, month text, shard int, reply_id timeuuid, " +
    "user_id bigint, author_name text, body text, reply_to_id timeuuid, created_at timestamp, " +
    "PRIMARY KEY ((comment_id, month, shard), reply_id)) " +
    "WITH CLUSTERING ORDER BY (reply_id ASC)",
    "CREATE TABLE IF NOT EXISTS " + K + ".reply_locations (" +
    "reply_id timeuuid PRIMARY KEY, comment_id timeuuid, month text, shard int)",
    "CREATE TABLE IF NOT EXISTS " + K + ".comment_likes (" +
    "comment_id timeuuid, shard int, account_id bigint, liked_at timestamp, " +
    "PRIMARY KEY ((comment_id, shard), account_id))",
    "CREATE TABLE IF NOT EXISTS " + K + ".comment_like_totals (" +
    "comment_id timeuuid PRIMARY KEY, total_likes bigint, refreshed_at timestamp)"
]

class CassandraDB:
    def __init__(self):
        self.cluster = None
        self.session = None

    def connect(self):
        last = None
        for _ in range(30):
            try:
                self.cluster = Cluster(contact_points=settings.hosts, port=settings.cassandra_port)
                self.session = self.cluster.connect()
                for statement in SCHEMA:
                    self.session.execute(statement)
                self.session.set_keyspace(K)
                self.session.row_factory = dict_factory
                return
            except Exception as exc:
                last = exc
                if self.cluster:
                    self.cluster.shutdown()
                    self.cluster = None
                time.sleep(5)
        raise RuntimeError(f"Cassandra connection failed: {last}")

    def execute(self, query, params=()):
        if self.session is None:
            raise RuntimeError("Cassandra is not connected")
        return self.session.execute(query, params)

    def close(self):
        if self.session:
            self.session.shutdown()
        if self.cluster:
            self.cluster.shutdown()

db = CassandraDB()
