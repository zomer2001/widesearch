# 抽取表单字段

本目录只定义“已有原文 → 字段抽取 → 知识库”的数据形状。SearchOS 的任务规划、搜索、抓取和采集字段不受影响。

## 文件

- `field_catalog.v1.json`：正式抽取字段目录，版本 `anduril_knowledge_fields_v1_20261104`。
- `html_row_mapping.v1.json`：按 `kind + group（中文组名）+ label（中文字段名）` 查找稳定 `field_id`。字段名相同但组不同有时不是同一含义，请不要只按字段名匹配。
- `extraction_result.schema.json`：一份原文对应的一份抽取结果结构。
- `blank_product.v1.json`、`blank_technology.v1.json`：空白表单展示模板。它们不是抽取结果的提交格式。
- `catalog_validation.json`：计数和跨组共用字段检查记录。
- `build_catalog.py`：仅使用 Python 标准库，从已确认 HTML 设计重建目录。不会访问模型、网络或数据库。

## 字段数量

原 HTML 有 430 个字段显示行：产品 290 行、技术 140 行。按语义整理后为 **408 个字段定义（产品 268，技术 140）**，含 **91 个可独立关联技术的具体功能字段**、39 个分组。

原设计页的“406”是草案统计，不能用来强制合并不相同的内容。本目录做了以下处理：

1. 长度、宽度、航程等跨产品形态共用的字段只定义一次，在多个适用组中引用，共归并 22 个重复显示行。
2. “其他功能”分别保留在 11 个功能组中。
3. 场景的“部署方式”（固定/机动等）与软件的“部署方式”（本地/私有云等）保留为两个字段。
4. 空中与水面/水下的“回收方式”共用一项，可存多个短词，适用组和来源保持可查。

## 抽取格式

一份来源输出一个对象：

```json
{
  "schema_version": "anduril_knowledge_fields_v1_20261104",
  "source_id": "原资料清单中的 source_id",
  "status": "extracted",
  "entities": [],
  "relations": [],
  "notes": []
}
```

实体内使用稀疏 `fields` 数组，只提交找到证据的字段。没有信息的字段不提交，**不要提交 `value: null`**；仅在构建空白表单和数据库展示时按目录补 `null` 并显示空白。目录的 `nullable` 是表单存储约定，`extraction_unknown_policy=omit` 是抽取提交约定。不得把“未知”“暂无”“—”当作已填写内容。`false` 必须有明确否定证据；没提到功能不是不支持。

每个字段事实记录 `field_id`、`value`、`quote`、`qualifiers`。`quote` 必须是当前来源原文中连续的一段；保存时与顶层 `source_id` 绑定。同一字段出现不同版本、条件、日期或矛盾数值时，分别保留事实，不覆盖成一个值。

字段类型：

| value_type | JSON 值 |
|---|---|
| boolean | `true` / `false`；无信息省略 |
| number | 数字 |
| measurement | `{"value": 20, "unit": "kg", "raw": "20 kg"}`；范围用 `min`/`max`，大于/小于/近似用 `operator`，非纯数字规格可仅保留 `raw` |
| partial_date | `YYYY`、`YYYY-MM` 或 `YYYY-MM-DD`；未知月日不得补齐 |
| short_text / text | 字符串；短字段优先一个词或短语 |
| text_list | 字符串数组 |
| url / url_list | 链接字符串 / 链接字符串数组 |
| entity_reference / entity_reference_list | 原文实体名称字符串 / 字符串数组，稍后解析到稳定实体 ID |

`examples` 是填写提示，**不是已核实的 Anduril 事实，也不是封闭词表**。除了真假值，`allowed_values` 当前均为 `null`，允许忠于原文的新短词。不要把举例中的数字、YOLO、协议或仓库自动填入资料没有提到的产品。

数字的测试条件、P95/均值/最大值、目标类型、版本和日期写入 `qualifiers`；不能把“最远”“最大”等限定丢掉。原文未给单位时保留 `unit: null`。参数不强制换算，以免损失原始含义。

`operator` 只允许 `=`、`>`、`>=`、`<`、`<=`、`approx`；没有限定时可省略。技术“频段”支持多个短词，值形如 `["LF", "VLF"]`。在原表单中找不到语义匹配的内容保留到 `unmapped-candidates.json`，不要强塞进相似字段；例如“笔记本/平板/头戴终端”并不等同于“固定/机动”的场景部署方式。

## 可重复行

模块和载荷分别使用 `repeat_group=components/payloads`；技术依赖、性能指标、参考仓库分别使用 `dependencies/metrics/implementations`。同一行中的所有字段使用同一个 `qualifiers.row_key`。例如同一个仓库的名称、链接、许可、固定版本必须归于同一行，不能把不同仓库的属性拼在一起。

`derived=true` 的“下级技术”“关联产品”由已有关系反向显示，不需要模型重复填。这些字段不会出现在空白输入模板中。

## 功能与技术关系

产品具体功能的支撑关系用 `source_key=产品实体`、`target_key=技术实体`，并在 `field_id` 中指定产品功能，如 `product_supports_publish_subscribe`。没有证据把某技术连到具体功能时，不能仅凭“发布订阅通常使用 MQTT/DDS”填产品已采用。

技术层级、依赖、产品谱系等关系也须有原文证据。参考实现可以记录在技术条目的仓库重复行中；**“存在可用开源实现”与“Anduril 使用该实现”分开**。本次没有运行项目，不能填写“本库已复现”。

关系类型与 `tools/build_review.py` 一致：`uses_technology`、`supports_function`、`subtechnology_of`、`implements`、`reference_for`、`variant_of`、`part_of`、`integrates`、`carries`、`deploys`、`evolves_from`、`replaces`。关系的 `field_id` 可为 `null`；非空只允许目录中 `is_function=true` 的产品字段、产品真假能力字段或对应组的“其他功能”，不能挂到名称或普通参数列。

## 重建检查

在本目录运行：

```bash
python3 build_catalog.py
```

成功会打印 `product=268`、`technology=140`、`total_fields=408`、`source_rows=430`、`specific_functions=91`。脚本检查未映射字段、ID 唯一性和命名格式；输出保留每个字段的原 HTML 分组、填写方式和示例，便于复核。
