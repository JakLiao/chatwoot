# frozen_string_literal: true

# ============================================================================
# reset_admin_password.rb · 重置采集账户的管理员密码
#
# 在 rails 容器里跑：
#   bundle exec rails runner /bootstrap/scripts/reset_admin_password.rb
#
# 幂等：每次跑都生成一个新密码。
# 密码生成规则满足 Chatwoot 的 devise 要求（6..128 位 + 大小写 + 数字 + 符号）。
#
# stdout 输出 ADMIN_PASSWORD=<明文>，由 scripts/40-reset-admin-password.sh
# 写进 keys/admin-password.txt（600）—— 不入库、不进 git。
# ============================================================================

require 'securerandom'

ADMIN_EMAIL = ENV.fetch('CHATWOOT_ADMIN_EMAIL', 'voice-admin@chatwoot.local')

def generate_password
  # 大写 + 小写 + 数字 + 符号，长度 24
  "Cs#{SecureRandom.hex(10)}9!"
end

user = User.find_by(email: ADMIN_EMAIL)
abort("找不到用户 #{ADMIN_EMAIL} —— 先跑 scripts/20-bootstrap-chatwoot.sh") if user.nil?

password = generate_password
user.password = password
user.password_confirmation = password
user.skip_confirmation!
user.confirmed_at ||= Time.current
user.save!

abort('密码重置失败') unless user.reload.valid_password?(password)

puts "ADMIN_PASSWORD=#{password}"
puts "[reset] #{ADMIN_EMAIL} 的密码已重置（user id=#{user.id}）"
