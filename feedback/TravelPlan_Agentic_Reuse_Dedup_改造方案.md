# TravelPlan Agentic + 数据复用 + 地点去重改造方案

## 1. 目标

这次改造解决三个核心问题：

1. **真正 Agentic**
   - TravelPlan 主规划要真正使用 SuperHarness Agent Loop / Tool Calling
   - 不再主要依赖“LLM 返回 JSON → Python 固定执行”

2. **已有数据优先复用**
   - 攻略预热、LLM 已解析地点、高德已验证信息能复用就复用
   - Agent 不允许每次重新搜一遍

3. **地点去重要独立于高德**
   - **不能把高德 `poi_id` 当成唯一实体 ID，也不能依赖高德来完成地点去重**
   - TravelPlan 自己维护 Canonical Place Entity

---

# 2. 当前问题

现在已经有：

```text
攻略预热
→ 搜攻略
→ LLM 抽地点
→ 高德验证
→ 入 City Cache
```

正式规划时也已经支持：

```text
先 City Cache
→ 有就复用
→ 没有才实时搜索
```

这个方向必须保留。

但还有两个风险。

第一，未来改成 Agentic 后，如果 Agent 可以直接调用：

```text
search_xiaohongshu
search_poi
get_poi_detail
```

很容易又变成：

```text
每次规划
→ Agent 重新搜索
→ 重复调用 Provider
→ 重复 LLM 抽取
→ 重复查高德
```

这样会更慢、更贵。

第二，地点去重目前不能依赖：

```text
高德 poi_id
```

因为同一个现实地点可能出现：

```text
不同查询返回不同 POI 记录
景区主 POI / 子 POI
入口 / 游客中心 / 景区本体
旧 ID / 新 ID
不同 Provider 不同 ID
```

所以：

> **高德 POI 只能作为一个证据，不是 TravelPlan 的唯一实体真相。**

---

# 3. 最终原则

整个系统统一遵守：

```text
LLM / Agent
= 决定需要什么信息

Reuse Layer
= 判断已有数据能不能直接用

Place Entity Resolver
= 判断是不是已有地点

ProviderHub
= 只有确实缺数据时才访问外部 Provider

Python Validator
= 最终硬校验
```

也就是说：

> **Agent 可以决定“我要查什么”，但不能决定“绕过缓存重新查”。**

所有 Tool 调用必须经过复用层。

---

# 4. 数据复用优先级

所有 Agent Tool 必须遵守：

```text
PrefetchBundle
↓
City Cache / Persistent Cache
↓
Query Cache
↓
Entity Resolver
↓
只有缺失时才调用 Provider
```

---

## 4.1 半静态数据

这些应该尽量长期复用：

```text
攻略正文
攻略检索词
LLM 已抽取地点
地点别名
景点 / 餐厅基础信息
坐标
行政区
商圈
高德基础 POI 信息
住宿区域知识
美食区域知识
```

流程：

```text
Prefetch
→ City Cache
→ Persistent Entity Store
→ Live Search
```

---

## 4.2 实时数据

这些不能长期复用：

```text
火车余票
火车价格
航班价格
酒店价格
酒店库存
门票实时价格
路线耗时
```

流程：

```text
当前 Session Prefetch
→ 短 TTL Cache
→ Real Provider
```

---

# 5. 地点不能靠高德 poi_id 去重

## 错误做法

```text
poi_id 相同
→ 同一个地点

poi_id 不同
→ 两个不同地点
```

这个不可靠。

例如同一个现实地点：

```text
成都大熊猫繁育研究基地
熊猫基地
成都熊猫基地
熊猫基地南门
熊猫基地游客中心
```

高德可能返回多个不同 POI。

因此：

```text
高德 poi_id
```

应该只是：

```text
provider_external_id
```

而不是：

```text
canonical_place_id
```

---

# 6. TravelPlan 自己维护 Canonical Place

建议内部统一使用：

```text
canonical_place_id
```

例如：

```text
place_canonical_000123
```

一个现实地点只有一个 TravelPlan canonical entity。

它下面可以挂：

```text
canonical_place_id = place_canonical_000123

canonical_name = 成都大熊猫繁育研究基地

aliases:
- 熊猫基地
- 成都熊猫基地
- 大熊猫基地
- 成都大熊猫繁育基地

provider_refs:
- amap: B0XXXX
- amap: B0YYYY
- other_provider: 12345
```

注意：

> 多个高德 POI ID 可以映射到同一个 canonical place。

---

# 7. 推荐的数据结构

## places

```text
canonical_place_id
canonical_name
normalized_name

city
district
business_area

lat
lng
address
category

confidence
created_at
updated_at
```

---

## place_aliases

```text
canonical_place_id
city
alias
normalized_alias
source
confidence
```

例如：

```text
place_001 | 成都 | 杜甫草堂
place_001 | 成都 | 成都杜甫草堂
place_001 | 成都 | 杜甫草堂景区
```

---

## place_provider_refs

```text
canonical_place_id
provider
provider_place_id
provider_name
lat
lng
address
fetched_at
```

例如：

```text
place_001
├─ amap:B001
├─ amap:B009
└─ other_provider:123
```

---

## place_evidence

```text
canonical_place_id
evidence_id
mention_text
confidence
```

同一个地点可以关联很多攻略。

---

## poi_query_cache

```text
city
normalized_query
query_type
matched_canonical_place_ids
provider_result_ids
fetched_at
expires_at
```

---

# 8. 地点解析流程

以后任何攻略抽出地点以后，都不要直接查高德。

统一经过：

```text
Place Entity Resolver
```

完整流程：

```text
LLM 抽出：
"成都杜甫草堂景区"

↓
Normalize

"杜甫草堂"

↓
Alias Index

是否已经能匹配已有 Canonical Place？
├─ YES
│   ↓
│   直接复用 canonical_place_id
│   ↓
│   新 Evidence 挂上去
│   ↓
│   不查高德
│
└─ NO
    ↓
    Query Cache

    以前是否查过类似 query？
    ├─ YES
    │   ↓
    │   尝试映射已有 Canonical Place
    │
    └─ NO
        ↓
        才查高德
        ↓
        高德返回候选
        ↓
        Entity Resolver 再判断
        ↓
        合并已有实体
        或
        创建新 Canonical Place
```

---

# 9. Entity Resolver 如何判断“是不是同一个地点”

不能只用一个字段。

应该使用多信号融合。

建议考虑：

```text
1. 城市
2. normalized name
3. aliases
4. 地址相似度
5. 经纬度距离
6. district
7. business_area
8. category
9. Provider 历史映射
10. 攻略共同上下文
11. 旧的人工/系统确认映射
```

---

## 9.1 强信号

例如：

```text
名称非常接近
+
坐标 100m 内
+
地址相似
```

可以高置信度合并。

---

## 9.2 中等信号

例如：

```text
熊猫基地
成都大熊猫繁育研究基地
```

名字不是完全一样。

但：

```text
city = 成都
category = 景区
地址一致
坐标非常接近
大量攻略语境一致
```

可以认为是同一个 canonical place。

---

## 9.3 冲突信号

例如：

```text
名称相似
但
坐标相距 8km
地址不同
```

不能合并。

宁可：

```text
保留两个实体
```

也不要错并。

原则：

> **漏合并比错合并安全。**

---

# 10. 景区主点和子点怎么处理

例如：

```text
成都大熊猫繁育研究基地
熊猫基地南门
熊猫基地游客中心
熊猫基地博物馆
```

这类不应该粗暴全部合成一个 Place。

建议增加：

```text
entity_relation
```

例如：

```text
熊猫基地
├─ 南门        child
├─ 游客中心    child
└─ 博物馆      child
```

数据结构：

```text
place_relations

parent_place_id
child_place_id
relation_type
```

relation_type 可以有：

```text
child
entrance
inside
branch
same_entity
nearby
```

这样：

```text
主景区
```

和：

```text
景区内部具体点
```

不会混成一个。

---

# 11. 连锁店 / 分店必须特别处理

例如：

```text
蜀大侠火锅
蜀大侠火锅春熙路店
蜀大侠火锅太古里店
```

不能因为 normalized name 很像就合并。

判断时：

```text
餐厅 / 酒店 / 门店
```

必须更重视：

```text
地址
坐标
门店后缀
商圈
```

因此不同类别要使用不同去重策略。

例如：

```text
景区
→ 名字 + 地址 + 坐标

餐厅
→ 地址 + 坐标 权重更高

酒店
→ 酒店 ID / 地址 / 坐标 / 名称

商圈
→ 名字 + 城市 + 区域范围
```

---

# 12. 攻略 Evidence 去重与地点去重分开

两个概念不能混。

## Evidence 去重

判断：

```text
是不是同一篇攻略
```

例如：

```text
URL
标题
作者
内容 hash
发布时间
```

---

## Place 去重

判断：

```text
攻略里说的地点是不是同一个现实实体
```

这两个必须分别处理。

正确关系：

```text
Canonical Place：杜甫草堂
│
├─ Evidence 1
├─ Evidence 2
├─ Evidence 3
└─ Evidence 4
```

不是：

```text
杜甫草堂
成都杜甫草堂
杜甫草堂景区
杜甫草堂博物馆
```

重复生成四个 Place。

---

# 13. 高德查询也要跨 Session / Run 复用

现在 CallLedger 主要能解决：

```text
同一次 Discovery
```

里的重复调用。

还应该加：

```text
跨 Session
跨 Run
```

复用。

例如第一次已经查过：

```text
成都 + 杜甫草堂
```

并且结果还新鲜。

下一次：

```text
成都 + 杜甫草堂
```

直接：

```text
poi_query_cache hit
```

不要再打高德。

---

# 14. Query Cache 不应该直接决定实体

Query Cache 只负责：

```text
“以前这个 query 查到了什么”
```

不能直接说：

```text
query 相同 = 同一个地点
```

正确流程：

```text
Query Cache
→ 返回历史候选
→ Entity Resolver
→ 确认 canonical place
```

---

# 15. 高德的作用应该变成“补证据”

以后高德主要负责：

```text
确认存在
补地址
补坐标
补行政区
补商圈
补营业时间
补路线
```

而不是：

```text
定义 TravelPlan 里什么是一个地点
```

所以推荐：

```text
TravelPlan Entity Store
= 主实体层

Amap
= Provider Evidence
```

---

# 16. Agentic Tool 必须经过 Resolver / Cache

未来 Planner Agent 调：

```text
search_poi("杜甫草堂")
```

不能直接：

```text
Agent
→ Amap
```

必须：

```text
Agent
↓
TravelPlan search_poi Tool
↓
Place Entity Resolver
↓
Alias Index
↓
Query Cache
↓
City Cache
↓
缺失？
├─ NO → 返回已有 Canonical Place
└─ YES
    ↓
    ProviderHub
    ↓
    Amap
    ↓
    Entity Resolver
    ↓
    更新 Canonical Place
```

---

# 17. 预热必须继续优先复用

当前预热模式必须保留：

```text
离线 / 后台预热
↓
攻略搜索
↓
LLM 抽取地点
↓
Entity Resolver
↓
必要时高德补证据
↓
City Cache / Entity Store
```

用户正式规划：

```text
先查预热结果
↓
命中
→ 直接用

缺少
→ 才补查
```

Agentic 改造不能破坏这一点。

---

# 18. 推荐最终架构

```text
用户
↓
Intent / Preference
↓
Prefetch
↓
TravelPlan Agent
↓
请求某类数据
↓
────────────────────────
Reuse & Entity Layer
  PrefetchBundle
  City Cache
  Alias Index
  Query Cache
  Canonical Place Store
  Entity Resolver
────────────────────────
↓
是否已有足够数据？
├─ YES
│   ↓
│   直接返回
│
└─ NO
    ↓
    ProviderHub
    ↓
    Amap / TikHub / 12306 / Tuniu
    ↓
    Entity Resolver
    ↓
    更新 Cache / Entity Store
↓
Planner Agent
↓
Python Validation
```

---

# 19. 和 SuperHarness 的关系

SuperHarness 负责：

```text
Agent Loop
Tool Calling
Capability Router
Plugin
MCP
Retry
Trace
Memory
Observability
```

TravelPlan 负责：

```text
Travel Tool
Reuse Policy
Entity Resolver
ProviderHub
Travel Planner
Hard Validation
```

也就是说：

```text
SuperHarness
= 怎么运行 Agent

TravelPlan
= Agent 在旅行领域怎么查数据、怎么复用、怎么判断实体
```

---

# 20. 开发优先级

## P0

先做：

```text
1. Canonical Place Entity
2. place_aliases
3. place_provider_refs
4. Place Entity Resolver
5. 所有 search_poi 前强制先 Resolver
6. Agent Tool 强制 Prefetch / Cache First
7. 高德不再作为唯一去重依据
```

---

## P1

然后：

```text
8. Cross-session Query Cache
9. 景区 parent / child 关系
10. 连锁门店特殊规则
11. Evidence → Canonical Place 重新挂载
12. Resolver Trace
```

---

## P2

最后：

```text
13. 低置信度 Entity Match 人工审计
14. 自动学习 Alias
15. Resolver Benchmark
16. 错并 / 漏并 Bad Case
```

---

# 21. Trace 必须能看见复用过程

例如：

```text
ASSISTANT
需要查询杜甫草堂

TOOL
resolve_place("成都", "成都杜甫草堂景区")

CACHE
Alias Match

RESULT
canonical_place_id = place_001
confidence = 0.96

PROVIDER
SKIPPED
reason = existing_entity_reused
```

或者：

```text
TOOL
resolve_place("成都", "某新地点")

CACHE
MISS

QUERY CACHE
MISS

PROVIDER
Amap search_poi

ENTITY RESOLVER
matched existing canonical place

RESULT
reuse place_018
```

这样后台可以真正看到：

```text
这次为什么没有重新查高德
```

---

# 22. 验收标准

至少覆盖下面这些情况。

### Case 1

```text
杜甫草堂
成都杜甫草堂
杜甫草堂景区
```

应该：

```text
一个 Canonical Place
多条 Alias
多条 Evidence
```

---

### Case 2

```text
熊猫基地
成都大熊猫繁育研究基地
```

如果多信号一致：

```text
合并
```

但不依赖高德 ID 是否相同。

---

### Case 3

```text
熊猫基地
熊猫基地南门
```

应该：

```text
两个实体
parent-child 关系
```

不能粗暴合并。

---

### Case 4

```text
海底捞春熙路店
海底捞太古里店
```

应该：

```text
两个 Place
```

即使名字很像。

---

### Case 5

第一次：

```text
成都 + 杜甫草堂
→ 查 Provider
```

第二次：

```text
成都 + 杜甫草堂
→ Cache / Resolver 命中
→ Provider 不再调用
```

---

### Case 6

已有预热：

```text
攻略
地点抽取
高德基础信息
```

正式规划：

```text
直接复用
```

只有需要：

```text
实时路线
实时票价
实时酒店
```

才重新访问 Provider。

---

# 23. 最终结论

这次改造最重要的三句话：

> **Agentic 不等于每次重新搜索。**

> **高德是地点信息 Provider，不是 TravelPlan 的实体数据库。**

> **TravelPlan 必须自己维护 Canonical Place + Alias + Entity Resolver，并且所有 Agent Tool 都必须先复用，再查询。**

最终目标：

```text
同一个现实地点
→ TravelPlan 里尽量只有一个 Canonical Entity

同一份攻略
→ 不重复存

同一个地点查过
→ 尽量不重复查 Provider

Agent
→ 只负责决定需要什么
→ 复用和去重由系统层保证
```
