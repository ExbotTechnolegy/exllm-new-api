const groupList = document.querySelector('#groups')
const modelList = document.querySelector('#models')
const search = document.querySelector('#search')
const status = document.querySelector('#status')
const number = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 5 })

let selected = ''

function element(tag, content, className) {
  const node = document.createElement(tag)
  node.textContent = content
  if (className) node.className = className
  return node
}

function discount(ratio) {
  if (!Number.isFinite(ratio) || ratio < 0) return '倍率待确认'
  if (ratio === 1) return '基础价'
  if (ratio > 1) return `基础价 × ${number.format(ratio)}`
  if (ratio === 0) return '0 倍率'
  return `${number.format(ratio * 10)} 折`
}

function render(data) {
  const vendors = new Map((Array.isArray(data.vendors) ? data.vendors : []).map(v => [v.id, v.name]))
  const { usable_group: names, group_ratio: ratios } = data
  const eligibleGroups = id => Object.hasOwn(names, id) && Object.hasOwn(ratios, id)
  const models = data.data.flatMap(model =>
    (Array.isArray(model.enable_groups) ? model.enable_groups : [])
      .filter(eligibleGroups)
      .map(group => ({ model, group, vendor: vendors.get(model.vendor_id) || '其他厂商' }))
  )

  function update() {
    const query = search.value.trim().toLowerCase()
    const rows = models.filter(({ model, group, vendor }) =>
      (!selected || group === selected) &&
      `${model.model_name} ${vendor}`.toLowerCase().includes(query)
    )
    status.textContent = rows.length ? `显示 ${rows.length} 项模型报价` : '没有符合条件的模型'
    modelList.replaceChildren(...rows.map(({ model, group, vendor }) => {
      const card = element('article', '')
      const details = element('a', '查看计费详情')
      details.href = '/pricing'
      card.append(
        element('h3', model.model_name),
        element('p', vendor),
        element('p', `${names[group]} · ${discount(ratios[group])}`, 'rate'),
        details,
      )
      return card
    }))
  }

  const buttons = [['', '全部分组'], ...Object.entries(names)
    .filter(([id]) => models.some(row => row.group === id))]
    .map(([id, name]) => {
      const button = element('button', name)
      button.type = 'button'
      if (id) button.append(element('span', discount(ratios[id])))
      button.setAttribute('aria-pressed', String(id === selected))
      button.addEventListener('click', () => {
        selected = id
        for (const sibling of groupList.children) {
          sibling.setAttribute('aria-pressed', String(sibling === button))
        }
        update()
      })
      return button
    })
  groupList.replaceChildren(...buttons)
  search.addEventListener('input', update)
  update()
}

try {
  const response = await fetch('/api/pricing', { cache: 'no-store' })
  if (!response.ok) throw new Error('Pricing request failed')
  const data = await response.json()
  if (!data.success || !Array.isArray(data.data) || !data.usable_group || !data.group_ratio) {
    throw new Error('Invalid pricing response')
  }
  render(data)
} catch {
  status.textContent = '暂时无法加载模型。请刷新页面重试。'
}
