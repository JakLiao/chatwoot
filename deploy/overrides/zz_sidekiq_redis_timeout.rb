# frozen_string_literal: true

# ============================================================================
# zz_sidekiq_redis_timeout.rb · 覆盖 Sidekiq 的 Redis 读超时
#
# 为什么需要这个文件：
#   Chatwoot 的 lib/redis/config.rb 把 Redis 超时硬编码成 1 秒
#   （url / password / ssl_params / reconnect_attempts / timeout: 1），
#   而 Sidekiq 7 的 reliable fetch 用阻塞读等待任务，服务端阻塞 2 秒。
#   客户端 1 秒就超时 —— 队列空闲时，每隔几秒就会刷一条：
#       ERROR  Error fetching job: Waited 3 seconds
#
#   功能上没有任何影响（任务照常被取走、被处理，队列不积压），
#   但这属于「红色日志」，会淹没真正的错误 —— 运维久了就麻木了。
#
#   redis-client 的 Config 只从显式参数取超时，URL 里带 ?timeout= 无效
#   （URLConfig 不解析 query），所以没法靠 .env 解决，只能在这里覆盖。
#
# 挂载方式（见 docker-compose.yml）：
#   ./overrides/zz_sidekiq_redis_timeout.rb:/app/config/initializers/zz_sidekiq_redis_timeout.rb:ro
#
# 生效顺序：Rails 按文件名排序加载初始化器，Chatwoot 自带的 sidekiq.rb 在前，
#          本文件（zz 开头）在后 —— 所以这里能覆盖掉它的设置。
#
# 去掉这个挂载 = 回到 Chatwoot 默认行为（只是日志变吵，功能不变）。
# ============================================================================

require Rails.root.join('lib/redis/config')

# 阻塞读要等满 2 秒，留足余量
SIDEKIQ_REDIS_TIMEOUT = 10

Sidekiq.configure_server do |config|
  config.redis = Redis::Config.app.merge(
    timeout: SIDEKIQ_REDIS_TIMEOUT,
    read_timeout: SIDEKIQ_REDIS_TIMEOUT,
    write_timeout: SIDEKIQ_REDIS_TIMEOUT
  )
end
