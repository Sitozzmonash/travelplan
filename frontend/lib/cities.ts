/**
 * 中国城市 / 目的地数据集（国内行程专用）。
 *
 * 结构说明（新增城市只需往 CHINA_CITIES 末尾追加一条，不需要改组件）：
 *   name    —— 展示名，直接出现在输入建议里，必须是用户认得出的写法，例如「成都」。
 *   region  —— 省级行政区（或直辖市 / 特别行政区），用于消歧与分组展示，例如「四川」。
 *   aliases —— 可选的常见别称、旧称或同义写法，仅参与匹配，不展示，例如「蓉城」。
 *
 * 为什么不做成枚举白名单：后端接受任意自然语言输入，
 * 这份数据只用于输入建议，既不校验也不限制用户输入一个不在列表里的城市。
 */
export interface ChinaCity {
  name: string;
  region: string;
  aliases?: string[];
}

export const CHINA_CITIES: ChinaCity[] = [
  { name: "北京", region: "北京", aliases: ["首都", "京"] },
  { name: "上海", region: "上海", aliases: ["沪"] },
  { name: "天津", region: "天津" },
  { name: "重庆", region: "重庆", aliases: ["渝"] },
  { name: "石家庄", region: "河北" },
  { name: "秦皇岛", region: "河北", aliases: ["北戴河"] },
  { name: "承德", region: "河北" },
  { name: "张家口", region: "河北" },
  { name: "保定", region: "河北" },
  { name: "邯郸", region: "河北" },
  { name: "太原", region: "山西" },
  { name: "大同", region: "山西" },
  { name: "平遥", region: "山西", aliases: ["平遥古城"] },
  { name: "呼和浩特", region: "内蒙古", aliases: ["呼市"] },
  { name: "包头", region: "内蒙古" },
  { name: "鄂尔多斯", region: "内蒙古" },
  { name: "呼伦贝尔", region: "内蒙古", aliases: ["海拉尔"] },
  { name: "沈阳", region: "辽宁" },
  { name: "大连", region: "辽宁" },
  { name: "丹东", region: "辽宁" },
  { name: "长春", region: "吉林" },
  { name: "延吉", region: "吉林", aliases: ["延边"] },
  { name: "长白山", region: "吉林", aliases: ["二道白河"] },
  { name: "哈尔滨", region: "黑龙江", aliases: ["冰城"] },
  { name: "齐齐哈尔", region: "黑龙江" },
  { name: "漠河", region: "黑龙江" },
  { name: "南京", region: "江苏", aliases: ["金陵"] },
  { name: "苏州", region: "江苏", aliases: ["姑苏"] },
  { name: "无锡", region: "江苏" },
  { name: "常州", region: "江苏" },
  { name: "扬州", region: "江苏" },
  { name: "镇江", region: "江苏" },
  { name: "徐州", region: "江苏" },
  { name: "南通", region: "江苏" },
  { name: "杭州", region: "浙江", aliases: ["西湖"] },
  { name: "宁波", region: "浙江" },
  { name: "温州", region: "浙江" },
  { name: "绍兴", region: "浙江" },
  { name: "嘉兴", region: "浙江", aliases: ["乌镇", "西塘"] },
  { name: "湖州", region: "浙江", aliases: ["莫干山"] },
  { name: "舟山", region: "浙江", aliases: ["普陀山"] },
  { name: "金华", region: "浙江", aliases: ["横店"] },
  { name: "丽水", region: "浙江" },
  { name: "合肥", region: "安徽" },
  { name: "黄山", region: "安徽", aliases: ["屯溪", "宏村"] },
  { name: "芜湖", region: "安徽" },
  { name: "福州", region: "福建", aliases: ["榕城"] },
  { name: "厦门", region: "福建", aliases: ["鹭岛", "鼓浪屿"] },
  { name: "泉州", region: "福建" },
  { name: "漳州", region: "福建", aliases: ["东山岛"] },
  { name: "南平", region: "福建", aliases: ["武夷山"] },
  { name: "南昌", region: "江西" },
  { name: "景德镇", region: "江西" },
  { name: "九江", region: "江西", aliases: ["庐山"] },
  { name: "上饶", region: "江西", aliases: ["婺源", "三清山"] },
  { name: "济南", region: "山东", aliases: ["泉城"] },
  { name: "青岛", region: "山东" },
  { name: "烟台", region: "山东", aliases: ["蓬莱"] },
  { name: "威海", region: "山东" },
  { name: "淄博", region: "山东" },
  { name: "潍坊", region: "山东" },
  { name: "泰安", region: "山东", aliases: ["泰山"] },
  { name: "济宁", region: "山东", aliases: ["曲阜"] },
  { name: "郑州", region: "河南" },
  { name: "洛阳", region: "河南", aliases: ["龙门石窟"] },
  { name: "开封", region: "河南" },
  { name: "安阳", region: "河南" },
  { name: "焦作", region: "河南", aliases: ["云台山"] },
  { name: "武汉", region: "湖北", aliases: ["江城"] },
  { name: "宜昌", region: "湖北", aliases: ["三峡"] },
  { name: "襄阳", region: "湖北" },
  { name: "恩施", region: "湖北" },
  { name: "长沙", region: "湖南" },
  { name: "张家界", region: "湖南" },
  { name: "湘西", region: "湖南", aliases: ["凤凰", "凤凰古城"] },
  { name: "衡阳", region: "湖南", aliases: ["南岳"] },
  { name: "广州", region: "广东", aliases: ["羊城"] },
  { name: "深圳", region: "广东", aliases: ["鹏城"] },
  { name: "珠海", region: "广东" },
  { name: "佛山", region: "广东", aliases: ["顺德"] },
  { name: "东莞", region: "广东" },
  { name: "惠州", region: "广东" },
  { name: "汕头", region: "广东" },
  { name: "潮州", region: "广东" },
  { name: "湛江", region: "广东" },
  { name: "南宁", region: "广西" },
  { name: "桂林", region: "广西", aliases: ["阳朔", "漓江"] },
  { name: "北海", region: "广西", aliases: ["涠洲岛"] },
  { name: "海口", region: "海南" },
  { name: "三亚", region: "海南" },
  { name: "万宁", region: "海南" },
  { name: "成都", region: "四川", aliases: ["蓉城", "春熙路"] },
  { name: "都江堰", region: "四川" },
  { name: "乐山", region: "四川", aliases: ["峨眉山"] },
  { name: "宜宾", region: "四川" },
  { name: "西昌", region: "四川", aliases: ["凉山"] },
  { name: "康定", region: "四川", aliases: ["甘孜"] },
  { name: "九寨沟", region: "四川", aliases: ["阿坝"] },
  { name: "贵阳", region: "贵州" },
  { name: "遵义", region: "贵州" },
  { name: "安顺", region: "贵州", aliases: ["黄果树"] },
  { name: "凯里", region: "贵州", aliases: ["黔东南", "西江千户苗寨"] },
  { name: "昆明", region: "云南", aliases: ["春城"] },
  { name: "大理", region: "云南" },
  { name: "丽江", region: "云南" },
  { name: "香格里拉", region: "云南", aliases: ["迪庆"] },
  { name: "景洪", region: "云南", aliases: ["西双版纳"] },
  { name: "腾冲", region: "云南", aliases: ["保山"] },
  { name: "玉溪", region: "云南" },
  { name: "拉萨", region: "西藏" },
  { name: "林芝", region: "西藏" },
  { name: "日喀则", region: "西藏" },
  { name: "西安", region: "陕西", aliases: ["长安"] },
  { name: "延安", region: "陕西" },
  { name: "咸阳", region: "陕西" },
  { name: "渭南", region: "陕西", aliases: ["华山"] },
  { name: "兰州", region: "甘肃" },
  { name: "敦煌", region: "甘肃", aliases: ["莫高窟"] },
  { name: "嘉峪关", region: "甘肃" },
  { name: "张掖", region: "甘肃" },
  { name: "西宁", region: "青海" },
  { name: "银川", region: "宁夏" },
  { name: "中卫", region: "宁夏", aliases: ["沙坡头"] },
  { name: "乌鲁木齐", region: "新疆", aliases: ["乌市"] },
  { name: "吐鲁番", region: "新疆" },
  { name: "喀什", region: "新疆" },
  { name: "伊宁", region: "新疆", aliases: ["伊犁"] },
  { name: "阿勒泰", region: "新疆", aliases: ["喀纳斯", "禾木"] },
  { name: "香港", region: "香港特别行政区" },
  { name: "澳门", region: "澳门特别行政区" },
];

const MAX_SUGGESTIONS = 8;

/**
 * 按关键字给出建议。匹配 name、region 与 aliases，前缀命中优先于包含命中，
 * 因此输入「成」时「成都」排在「宜宾」这类只靠别名命中的条目之前。
 */
export function searchCities(keyword: string, limit = MAX_SUGGESTIONS): ChinaCity[] {
  const query = keyword.trim().toLowerCase();
  if (!query) return [];

  const scored: { city: ChinaCity; score: number }[] = [];
  for (const city of CHINA_CITIES) {
    const score = matchScore(city, query);
    if (score > 0) scored.push({ city, score });
  }

  return scored
    .sort((left, right) => right.score - left.score || left.city.name.localeCompare(right.city.name, "zh-CN"))
    .slice(0, limit)
    .map((entry) => entry.city);
}

function matchScore(city: ChinaCity, query: string): number {
  const fields = [city.name, city.region, ...(city.aliases ?? [])];
  let best = 0;
  for (const field of fields) {
    const value = field.toLowerCase();
    if (value === query) best = Math.max(best, 4);
    else if (value.startsWith(query)) best = Math.max(best, 3);
    else if (value.includes(query)) best = Math.max(best, 2);
  }
  return best;
}
