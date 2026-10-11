/** 求职方向模板：为不同岗位提供示例要点、填写提示和推荐技能。 */

export interface JobTemplate {
  key: string;
  label: string;
  /** 经历要点（bullet）的填写提示 */
  bulletHint: string;
  /** 示例要点，用于引导用户照着写 */
  exampleBullets: string[];
  /** 该岗位常用技能，点击可快速添加 */
  recommendedSkills: string[];
}

export const JOB_TEMPLATES: JobTemplate[] = [
  {
    key: "tech",
    label: "技术研发",
    bulletHint: "动词 + 技术栈 + 量化成果",
    exampleBullets: [
      "主导 XX 系统后端开发，基于 Python + FastAPI 支撑日均 10 万请求",
      "引入 Redis 缓存与异步化，接口响应时间从 800ms 降至 100ms",
      "设计微服务拆分方案，团队 5 人协作，沉淀架构设计文档",
    ],
    recommendedSkills: [
      "Python", "Java", "Go", "MySQL", "Redis", "Docker", "Kubernetes",
      "Linux", "Git", "FastAPI", "Spring Boot", "React", "Vue",
    ],
  },
  {
    key: "product",
    label: "产品经理",
    bulletHint: "动词 + 产品动作 + 数据结果",
    exampleBullets: [
      "负责 XX 功能从 0 到 1 规划，撰写 PRD 并推动上线，DAU 提升 30%",
      "通过用户访谈与数据分析定位需求，输出优先级排序，推动 3 个版本迭代",
      "协调设计、研发、测试多方，保障项目按期交付，减少需求返工 20%",
    ],
    recommendedSkills: [
      "需求分析", "PRD 撰写", "Axure", "Figma", "用户研究",
      "竞品分析", "项目管理", "A/B 测试", "数据分析",
    ],
  },
  {
    key: "design",
    label: "设计",
    bulletHint: "动词 + 设计产出 + 影响/成果",
    exampleBullets: [
      "负责 XX App 视觉改版，基于 Figma 输出设计规范，用户满意度提升 25%",
      "搭建组件库与设计系统，跨端复用组件 60+，缩短设计周期 40%",
      "主导用户调研与可用性测试，优化关键路径，转化率提升 15%",
    ],
    recommendedSkills: [
      "Figma", "Sketch", "Photoshop", "Illustrator", "设计系统",
      "用户研究", "交互设计", "视觉设计", "After Effects",
    ],
  },
  {
    key: "operation",
    label: "运营",
    bulletHint: "动词 + 运营手段 + 数据结果",
    exampleBullets: [
      "负责公众号内容运营，策划选题 50+ 篇，粉丝增长 3 万，平均阅读 8000+",
      "策划线上活动，通过裂变玩法带来新增用户 2 万，获客成本降低 35%",
      "搭建用户分层体系与精细化运营策略，次日留存率提升 12%",
    ],
    recommendedSkills: [
      "内容运营", "用户运营", "活动策划", "社群运营", "公众号",
      "短视频", "文案撰写", "用户增长", "活动复盘",
    ],
  },
  {
    key: "marketing",
    label: "市场/销售",
    bulletHint: "动词 + 营销动作 + 业绩数字",
    exampleBullets: [
      "负责华东区域渠道拓展，签约客户 30 家，年度业绩达成 120%",
      "制定并落地市场推广方案，线索量提升 45%，转化率提升 8%",
      "维护重点客户关系，客户续约率 90%，复购金额同比增长 25%",
    ],
    recommendedSkills: [
      "渠道拓展", "商务谈判", "市场推广", "客户关系", "销售漏斗",
      "市场调研", "品牌策划", "大客户销售", "CRM",
    ],
  },
  {
    key: "finance",
    label: "金融/财会",
    bulletHint: "动词 + 专业动作 + 成果",
    exampleBullets: [
      "参与 XX 项目财务尽调，梳理目标公司 3 年报表，识别 5 处财务风险",
      "搭建月度经营分析模型，输出管理层报表，辅助决策并节省成本 15%",
      "负责报销流程与税务申报，确保合规，差错率降至 0",
    ],
    recommendedSkills: [
      "财务分析", "会计", "税务", "审计", "估值建模", "财务报表",
      "成本核算", "CPA", "CFA", "Excel",
    ],
  },
  {
    key: "legal",
    label: "法律",
    bulletHint: "动词 + 法律工作 + 成果",
    exampleBullets: [
      "协助处理 XX 合同审查 100+ 份，识别并规避 8 处重大法律风险",
      "参与尽职调查项目，出具法律意见书，支撑交易顺利完成",
      "跟踪研究行业法规动态，输出合规建议，助力业务合规落地",
    ],
    recommendedSkills: [
      "法律检索", "合同审查", "尽职调查", "法律意见书", "合规",
      "法考", "案例分析", "文书写作",
    ],
  },
  {
    key: "education",
    label: "教育/教师",
    bulletHint: "动词 + 教学动作 + 成果",
    exampleBullets: [
      "负责 XX 学科教学工作，班级平均分提升 15 分，年级排名前 3",
      "设计分层教学方案与课件，覆盖学生 200+，课堂满意度 95%",
      "组织教研活动 10+ 场，沉淀校本教材，获校级教学成果奖",
    ],
    recommendedSkills: [
      "教学设计", "课件制作", "教研", "班级管理", "教师资格证",
      "课程开发", "普通话", "课堂管理",
    ],
  },
  {
    key: "electrical",
    label: "电气/自动化",
    bulletHint: "动词 + 专业动作 + 量化成果",
    exampleBullets: [
      "负责 XX 生产线电气系统设计与调试，投运后故障率降低 40%",
      "编写 PLC 控制程序完成产线自动化改造，单班人工减少 3 人",
      "设计供配电方案与电气原理图，项目一次通过验收",
    ],
    recommendedSkills: [
      "PLC", "电气设计", "AutoCAD", "EPLAN", "电气原理图", "继电保护",
      "供配电", "电机控制", "变频器", "单片机", "嵌入式", "MATLAB",
    ],
  },
  {
    key: "mechanical",
    label: "机械/制造",
    bulletHint: "动词 + 专业动作 + 量化成果",
    exampleBullets: [
      "负责 XX 设备机械结构设计，完成 3D 建模与工程图，样机一次装配成功",
      "通过有限元分析优化关键部件，减重 15% 且满足安全系数",
      "制定零部件加工工艺，良品率从 92% 提升到 98%",
    ],
    recommendedSkills: [
      "机械设计", "SolidWorks", "AutoCAD", "UG/NX", "Creo", "ANSYS",
      "有限元分析", "机械制图", "公差配合", "工艺设计", "CATIA", "CNC",
    ],
  },
  {
    key: "medical",
    label: "医学护理",
    bulletHint: "动词 + 专业动作 + 量化成果",
    exampleBullets: [
      "在 XX 医院实习，协助完成 XX 例病例记录与基础护理，患者满意度 95%",
      "规范执行静脉穿刺、无菌操作等基础护理 20+ 项，无差错事故",
      "参与病房健康宣教与患者沟通，整理护理文书，获带教老师好评",
    ],
    recommendedSkills: [
      "基础护理", "无菌操作", "静脉穿刺", "健康宣教", "病例记录",
      "医患沟通", "急救技能", "护理文书", "生命体征监测",
    ],
  },
  {
    key: "civil",
    label: "土木/建筑",
    bulletHint: "动词 + 专业动作 + 量化成果",
    exampleBullets: [
      "参与 XX 项目结构设计，用 PKPM/YJK 完成建模计算，通过施工图审查",
      "负责现场施工管理与进度协调，协调 4 个班组，工期提前 15 天完工",
      "用 CAD/BIM 完成图纸深化与工程量计算，材料损耗降低 8%",
    ],
    recommendedSkills: [
      "结构设计", "AutoCAD", "PKPM", "YJK", "BIM", "Revit",
      "施工管理", "工程测量", "造价", "广联达", "土木工程材料",
    ],
  },
  {
    key: "media",
    label: "新闻/传媒",
    bulletHint: "动词 + 内容动作 + 传播数据",
    exampleBullets: [
      "负责校园媒体选题策划与采写，产出稿件 30+ 篇，单篇最高阅读 5 万",
      "策划短视频栏目，剪辑发布 20 期，全网播放量 100 万+",
      "运营公众号与短视频账号，粉丝增长 2 万，互动率提升 40%",
    ],
    recommendedSkills: [
      "选题策划", "新闻采写", "视频剪辑", "短视频运营", "公众号运营",
      "采访", "文案", "Premiere", "剪映", "舆情分析",
    ],
  },
  {
    key: "environmental",
    label: "环境工程",
    bulletHint: "动词 + 专业动作 + 量化成果",
    exampleBullets: [
      "参与 XX 污水处理项目，负责水质监测与数据分析，出水达标率 100%",
      "协助编制环评报告与环保方案，完成 XX 项目环境影响评价",
      "开展污染源调查与采样分析，出具检测报告 50+ 份",
    ],
    recommendedSkills: [
      "环境监测", "水处理", "环境影响评价", "环评报告", "水质分析",
      "AutoCAD", "实验操作", "采样分析", "大气污染防治", "固废处理",
    ],
  },
  {
    key: "general",
    label: "通用",
    bulletHint: "动词 + 方法 + 量化成果",
    exampleBullets: [
      "负责 XX 工作的统筹与执行，通过流程优化将效率提升 30%",
      "跨部门协作推进项目落地，协调 5 个团队按时交付",
      "梳理并沉淀工作文档，形成标准化流程，新人上手时间缩短 50%",
    ],
    recommendedSkills: [
      "沟通协调", "团队协作", "项目管理", "时间管理", "问题解决",
      "学习能力", "文档撰写", "办公软件",
    ],
  },
];
