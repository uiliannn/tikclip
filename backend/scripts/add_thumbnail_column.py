#!/usr/bin/env python3
"""
添加thumbnail字段到projects表的脚本
"""

import sys
from pathlib import Path

# 添加backend目录到Python路径
backend_dir = Path(__file__).parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from backend.core.database import engine
from sqlalchemy import text

def add_thumbnail_column():
    """添加thumbnail字段到projects表"""
    try:
        # 检查字段是否已存在
        with engine.connect() as conn:
            # 对于SQLite，检查表结构
            result = conn.execute(text("PRAGMA table_info(projects)"))
            columns = [row[1] for row in result.fetchall()]
            
            if 'thumbnail' in columns:
                print("✅ thumbnail字段已存在，无需添加")
                return True
            
            # 添加thumbnail字段
            conn.execute(text("ALTER TABLE projects ADD COLUMN thumbnail TEXT"))
            conn.commit()
            print("✅ 成功添加thumbnail字段到projects表")
            return True
            
    except Exception as e:
        print(f"❌ 添加thumbnail字段失败: {e}")
        return False

def main():
    """主函数"""
    print("🚀 开始添加thumbnail字段...")
    
    if add_thumbnail_column():
        print("🎉 thumbnail字段添加完成！")
    else:
        print("❌ thumbnail字段添加失败")
        sys.exit(1)

if __name__ == "__main__":
    main()
