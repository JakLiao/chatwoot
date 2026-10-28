# frozen_string_literal: true

# ============================================================================
# bootstrap_chatwoot.rb · Chatwoot 客户声音收集系统 · 一次性引导初始化
#
# 在 rails 容器里跑：
#   bundle exec rails runner /bootstrap/scripts/bootstrap_chatwoot.rb
#
# 做四件事（全部幂等，重复执行安全）：
#   1. 建「客户声音中心」账户 + 管理员用户（账户级 administrator）
#   2. 拿到该用户的 Application API access_token
#   3. 用**真实 API** 建好采集用的 API 频道收件箱（message_type=incoming 只有它支持）
#   4. 建好会话/联系人的自定义属性定义，以及每个来源系统的标签
#
# 第 3、4 步刻意走 HTTP API 而不是直接写模型 —— 这样顺带把「access_token 能调
# Application API」这条链路验证了一遍（社区版即可用，不需要企业版 license）。
#
# 跑完在 stdout 打印：
#   ---CHATWOOT_ENV---
#   CHATWOOT_ACCOUNT_ID=1
#   CHATWOOT_INBOX_ID=1
#   CHATWOOT_ACCESS_TOKEN=xxxx
#   ---END---
# 由 scripts/20-bootstrap-chatwoot.sh 负责回填 .env
# ============================================================================

require 'json'
require 'net/http'
require 'securerandom'
require 'uri'
require 'yaml'

ACCOUNT_NAME = ENV.fetch('CHATWOOT_ACCOUNT_NAME', '客户声音中心')
ADMIN_EMAIL  = ENV.fetch('CHATWOOT_ADMIN_EMAIL', 'voice-admin@chatwoot.local')
ADMIN_NAME   = ENV.fetch('CHATWOOT_ADMIN_NAME', '客户声音管理员')
INBOX_NAME   = ENV.fetch('CHATWOOT_INBOX_NAME', '客户声音采集')
API_BASE     = ENV.fetch('CHATWOOT_API_BASE', 'http://rails:3000').sub(%r{/$}, '')
REGISTRY     = ENV.fetch('INTAKE_REGISTRY_PATH', '/bootstrap/gateway/config/source_systems.yml')

def say(message)
  puts "[bootstrap] #{message}"
end

# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------
def api_post(path, body, token)
  # ⚠️ 不要用 URI.join —— path 以 / 开头时会把 /api/v1 前缀整个冲掉
  uri = URI.parse("#{API_BASE}/api/v1#{path}")
  request = Net::HTTP::Post.new(uri)
  request['Content-Type'] = 'application/json'
  request['Accept'] = 'application/json'
  request['api_access_token'] = token
  request.body = JSON.generate(body)

  response = Net::HTTP.start(uri.host, uri.port, open_timeout: 5, read_timeout: 30) do |http|
    http.request(request)
  end

  parsed = begin
    JSON.parse(response.body)
  rescue JSON::ParserError
    { 'raw' => response.body.to_s[0, 300] }
  end
  [response.code.to_i, parsed]
end

# 422 多半是「已存在」，当成成功处理
def ensure_post(label, path, body, token)
  status, payload = api_post(path, body, token)
  case status
  when 200..299
    say("✔ #{label}")
    payload
  when 422
    say("· #{label}（已存在，跳过）")
    payload
  else
    say("✘ #{label} 失败 HTTP #{status}：#{payload.to_s[0, 300]}")
    nil
  end
end

# --------------------------------------------------------------------------
# 1. 账户 + 管理员用户
# --------------------------------------------------------------------------
def build_admin_password
  # Chatwoot 的 devise 要求 6..128 位，且 password_has_required_content
  # 需要大小写 + 数字 + 符号 —— 这里一次性凑齐
  "Cs#{SecureRandom.hex(12)}9!"
end

account = Account.order(:id).first

user = User.find_by(email: ADMIN_EMAIL)
generated_password = nil
if user.nil?
  password = build_admin_password
  generated_password = password
  user = User.new(
    name: ADMIN_NAME,
    email: ADMIN_EMAIL,
    password: password,
    password_confirmation: password
  )
  user.skip_confirmation!
  user.save!
  say("已创建管理员用户 #{ADMIN_EMAIL}（id=#{user.id}）")
else
  say("管理员用户已存在 #{ADMIN_EMAIL}（id=#{user.id}）")
end

if account.nil?
  # ⚠️ AccountBuilder 的 email 参数是必填（pattr_initialize 里的 email!），
  #    即使已经带了 user 也得传 —— 否则 AttrExtras 直接报 Missing required keys
  builder = AccountBuilder.new(account_name: ACCOUNT_NAME, email: ADMIN_EMAIL, user: user)
  builder.perform
  account = Account.order(:id).first
  say("已创建账户 #{ACCOUNT_NAME}（id=#{account.id}）")
else
  say("账户已存在 #{account.name}（id=#{account.id}）")
end

# 账户级管理员（决定能不能建收件箱 / 看全部会话）
link = AccountUser.find_or_initialize_by(account_id: account.id, user_id: user.id)
link.role = :administrator
link.save!

Current.account = account
Current.user = user

# --------------------------------------------------------------------------
# 2. Application API access_token
# --------------------------------------------------------------------------
token = user.access_token
if token.nil?
  token = AccessToken.create!(owner: user)
end
say("access_token = #{token.token}")

# --------------------------------------------------------------------------
# 3. API 频道收件箱
#
# message_type = 'incoming' 只有 Channel::Api 支持 —— 客户反馈必须走这个频道，
# 普通 Web 收件箱建出来的会话收不进 incoming 消息。
# --------------------------------------------------------------------------
inbox = account.inboxes.find_by(channel_type: 'Channel::Api')
if inbox.nil?
  status, payload = api_post(
    "/accounts/#{account.id}/inboxes",
    { 'name' => INBOX_NAME, 'channel' => { 'type' => 'api' } },
    token.token
  )
  if (200..299).cover?(status)
    inbox_id = payload.is_a?(Hash) ? (payload['id'] || payload.dig('payload', 'inbox', 'id')) : nil
    inbox = account.inboxes.find_by(id: inbox_id)
    say("已创建 API 收件箱 #{INBOX_NAME}（id=#{inbox_id}）")
  else
    say("✘ 创建 API 收件箱失败 HTTP #{status}：#{payload.to_s[0, 300]}")
  end
else
  say("API 收件箱已存在 #{inbox.name}（id=#{inbox.id}）")
end

abort('API 收件箱创建失败，后续无法采集') if inbox.nil?

# --------------------------------------------------------------------------
# 4. 自定义属性定义 + 标签
# --------------------------------------------------------------------------
CONVERSATION_ATTRIBUTES = [
  { 'attribute_key' => 'source_system', 'attribute_display_name' => '来源系统', 'attribute_display_type' => 'text' },
  { 'attribute_key' => 'feedback_type', 'attribute_display_name' => '反馈类型', 'attribute_display_type' => 'text' },
  { 'attribute_key' => 'sentiment', 'attribute_display_name' => '情绪', 'attribute_display_type' => 'text' },
  { 'attribute_key' => 'rating', 'attribute_display_name' => '评分', 'attribute_display_type' => 'number' },
  { 'attribute_key' => 'channel', 'attribute_display_name' => '来源渠道', 'attribute_display_type' => 'text' },
  { 'attribute_key' => 'request_id', 'attribute_display_name' => '请求ID', 'attribute_display_type' => 'text' },
  { 'attribute_key' => 'occurred_at', 'attribute_display_name' => '发生时间', 'attribute_display_type' => 'text' }
].freeze

CONVERSATION_ATTRIBUTES.each do |attribute|
  ensure_post(
    "会话属性 #{attribute['attribute_key']}",
    "/accounts/#{account.id}/custom_attribute_definitions",
    { 'custom_attribute_definition' => attribute.merge('attribute_model' => 'conversation_attribute') },
    token.token
  )
end

ensure_post(
  '联系人属性 source_system',
  "/accounts/#{account.id}/custom_attribute_definitions",
  {
    'custom_attribute_definition' => {
      'attribute_key' => 'source_system',
      'attribute_display_name' => '来源系统',
      'attribute_display_type' => 'text',
      'attribute_model' => 'contact_attribute'
    }
  },
  token.token
)

# 标签：每个已登记的来源系统一个，外加反馈类型标签
registry = begin
  YAML.safe_load(File.read(REGISTRY)) || {}
rescue StandardError => e
  say("⚠ 读取注册表失败：#{e.message}")
  { 'sources' => [] }
end

sources = registry['sources'] || []
sources.each do |source|
  label = source['label'].to_s
  next if label.empty?

  ensure_post(
    "标签 #{label}",
    "/accounts/#{account.id}/labels",
    { 'label' => { 'title' => label } },
    token.token
  )
end

# --------------------------------------------------------------------------
# 5. 回填 .env 需要的值
# --------------------------------------------------------------------------
puts '---CHATWOOT_ENV---'
puts "CHATWOOT_ACCOUNT_ID=#{account.id}"
puts "CHATWOOT_INBOX_ID=#{inbox.id}"
puts "CHATWOOT_ACCESS_TOKEN=#{token.token}"
# 只有「新建用户」这一轮才有明文密码；重跑时为空
puts "ADMIN_PASSWORD=#{generated_password}"
puts '---END---'

say('引导初始化完成')
