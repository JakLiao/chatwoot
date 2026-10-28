# frozen_string_literal: true

# ============================================================================
# cleanup_test_data.rb · 清理自检脚本产生的测试数据
#
# scripts/30-verify.sh 每次都会提交一条测试反馈；跑完自动清掉，
# 保证系统里不留垃圾数据。
#
# 只删「会话的 contact_inbox.source_id 以 TEST_PREFIX 开头」的会话，
# 以及因此产生、且没有其它会话的联系人。
# 账户 / 收件箱 / 标签 / 自定义属性一律保留。
#
# 用法：
#   docker compose run --rm -v "$PWD:/bootstrap:ro" \
#     rails bundle exec rails runner /bootstrap/scripts/cleanup_test_data.rb
# ============================================================================

# 清理几类测试数据：
#   company_xianxing:verify-*            自检脚本提交的
#   company_xianxing:auto_*              无 request_id 时自动推导幂等键的边界用例
#   company_xianxing:casdoor-probe-*     Casdoor 用户名 / UUID 两条路径的探测
#   company_xianxing:uuid-probe-*        UUID 路径探测（source_id 前缀）
#   company_xianxing:xianxing-flow-probe-*  显形侧字段映射的端到端验证
TEST_PREFIXES = ENV.fetch(
  'TEST_SOURCE_ID_PREFIXES',
  'company_xianxing:verify-,company_xianxing:auto_,' \
  'company_xianxing:casdoor-probe-,company_xianxing:uuid-probe-,' \
  'company_xianxing:xianxing-flow-probe-'
).split(',').map(&:strip).reject(&:empty?)

account = Account.order(:id).first
abort('没有找到账户 —— 先跑 20-bootstrap-chatwoot.sh') if account.nil?

contact_inbox_ids = ContactInbox
                    .joins(:inbox)
                    .where(inboxes: { account_id: account.id })
                    .where(TEST_PREFIXES.map { |p| "contact_inboxes.source_id LIKE ?" }.join(' OR '),
                          *TEST_PREFIXES.map { |p| "#{p}%" })
                    .pluck(:id)

if contact_inbox_ids.empty?
  puts '[cleanup] 没有需要清理的测试数据'
else
  conversations = Conversation.where(contact_inbox_id: contact_inbox_ids)
  conversation_ids = conversations.pluck(:id)
  contact_ids = conversations.pluck(:contact_id).uniq

  puts "[cleanup] 删除 #{conversation_ids.size} 条测试会话"
  conversations.destroy_all

  # 只删「没有任何会话残留」的联系人
  orphan_contacts = Contact.where(id: contact_ids)
                           .where.not(id: Conversation.where(contact_id: contact_ids).select(:contact_id))
  puts "[cleanup] 删除 #{orphan_contacts.count} 个残留联系人"
  orphan_contacts.destroy_all

  # 回收 contact_inbox
  stale = ContactInbox.where(id: contact_inbox_ids)
  puts "[cleanup] 回收 #{stale.count} 条 contact_inbox"
  stale.destroy_all
end

puts '[cleanup] 完成'
