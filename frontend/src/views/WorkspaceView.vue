<script setup>
import { computed, onMounted, ref } from "vue";
import {
  Check,
  KeyRound,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
  X,
} from "lucide-vue-next";
import { api, request } from "../services/api";
import { usePlatformStore } from "../stores/platform";
import { confirmDialog, promptDialog } from "../services/dialog";
import { formatBeijingDateTime } from "../services/dateFormatting";

const store = usePlatformStore();
const me = ref({});
const users = ref([]);
const members = ref([]);
const invitations = ref([]);
const candidates = ref([]);
const workspaceName = ref("");
const inviteName = ref("");
const inviteEdit = ref(false);
const newUsername = ref("");
const newPassword = ref("");
const busy = ref(false);
const userModal = ref(false);
const inviteModal = ref(false);
const workspaceModal = ref(false);
const inviteCandidates = computed(() => candidates.value);
const canManage = computed(
  () => store.currentWorkspace?.owner_id === me.value.id,
);
async function load() {
  busy.value = true;
  try {
    me.value = await request("/api/auth/me");
    invitations.value = await api.invitations();
    if (me.value.is_admin) users.value = await api.users();
    members.value = store.currentWorkspace
      ? (await api.workspaceMembers(store.currentWorkspace.id)).members
      : [];
    candidates.value = canManage.value && store.currentWorkspace
      ? await api.workspaceInviteCandidates(store.currentWorkspace.id)
      : [];
  } catch (error) {
    store.error = error.message;
  } finally {
    busy.value = false;
  }
}
onMounted(async () => {
  if (!store.currentWorkspace) await store.load();
  await load();
});
async function createWorkspace() {
  if (!workspaceName.value.trim()) return;
  try {
    await api.createWorkspace(workspaceName.value.trim());
    workspaceName.value = "";
    await store.load();
    await load();
    workspaceModal.value = false;
    store.notify("工作空间已创建");
  } catch (error) {
    store.error = error.message;
  }
}
async function deleteWorkspace(item) {
  if (item.owner_id !== me.value.id) return;
  if (!(await confirmDialog({
    title: "删除工作空间",
    message: `删除工作空间“${item.name}”？其中所有知识库、文件、模型配置、智能体和运行记录都会永久删除。`,
    confirmLabel: "永久删除",
    danger: true,
  }))) return;
  try {
    await api.deleteWorkspace(item.id);
    localStorage.removeItem("xuanshu_workspace");
    await store.load();
    await load();
    store.notify("工作空间及全部内容已删除");
  } catch (error) {
    store.error = error.message;
  }
}
async function invite() {
  try {
    await api.inviteMember(
      store.currentWorkspace.id,
      inviteName.value.trim(),
      inviteEdit.value,
    );
    inviteName.value = "";
    await load();
    inviteModal.value = false;
  } catch (error) {
    store.error = error.message;
  }
}
async function changePermission(member) {
  try {
    await api.setMemberPermission(
      store.currentWorkspace.id,
      member.user_id,
      !member.can_edit,
    );
    await load();
  } catch (error) {
    store.error = error.message;
  }
}
async function decide(item, decision) {
  try {
    await api.decideInvitation(item.id, decision);
    await store.load();
    await load();
  } catch (error) {
    store.error = error.message;
  }
}
async function createUser() {
  if (!newUsername.value.trim() || newPassword.value.length < 8) {
    store.error = "用户名不能为空，密码至少 8 位";
    return;
  }
  try {
    await api.createUser(newUsername.value.trim(), newPassword.value);
    newUsername.value = "";
    newPassword.value = "";
    await load();
    userModal.value = false;
    store.notify("子账号已创建");
  } catch (error) {
    store.error = error.message;
  }
}
function openInvite() { inviteName.value = ""; inviteEdit.value = false; inviteModal.value = true; }
async function resetPassword(item) {
  const password = await promptDialog({
    title: "重置用户密码",
    message: `为 ${item.username} 设置新密码。`,
    inputLabel: "新密码",
    inputType: "password",
    inputMinLength: 8,
    placeholder: "至少 8 位",
    confirmLabel: "重置密码",
  });
  if (password === null) return;
  if (password.length < 8) {
    store.error = "密码至少 8 位";
    return;
  }
  try {
    await api.resetUserPassword(item.id, password);
    store.notify(`${item.username} 的密码已重置`);
  } catch (error) {
    store.error = error.message;
  }
}
async function deleteUser(item) {
  if (!(await confirmDialog({
    title: "删除用户账号",
    message: `删除账号“${item.username}”？该账号拥有的全部工作空间及其中所有数据都会永久删除。`,
    confirmLabel: "永久删除",
    danger: true,
  }))) return;
  try {
    await api.deleteUser(item.id);
    await store.load();
    await load();
    store.notify("账号及其所有工作空间已删除");
  } catch (error) {
    store.error = error.message;
  }
}
function chooseWorkspace(item) {
  localStorage.setItem("xuanshu_workspace", item.id);
  store.load().then(load);
}
</script>

<template>
  <div class="page-heading">
    <div>
      <h2>用户与工作空间</h2>
      <p>隔离管理模型、知识库、Skills、Tools、智能体和成员权限。</p>
    </div>
    <button class="button" :disabled="busy" @click="load">
      <RefreshCw :size="14" />刷新
    </button>
  </div>
  <section v-if="invitations.length" class="panel workspace-section">
    <header class="panel-header">
      <h3>待处理邀请</h3>
      <span>{{ invitations.length }}</span>
    </header>
    <div class="invitation-list">
      <article v-for="item in invitations" :key="item.id">
        <div>
          <strong>{{ item.workspace_name }}</strong
          ><small
            >{{ item.inviter }} 邀请你加入 ·
            {{ item.can_edit ? "可编辑" : "仅使用" }}</small
          >
        </div>
        <button class="button" @click="decide(item, 'reject')">
          <X :size="13" />拒绝</button
        ><button class="button primary" @click="decide(item, 'accept')">
          <Check :size="13" />接受
        </button>
      </article>
    </div>
  </section>
  <div class="settings-grid">
    <section class="panel workspace-section">
      <header class="panel-header">
        <h3>工作空间</h3>
        <button class="button small primary" @click="workspaceName=''; workspaceModal=true">
          <Plus :size="14" />新建工作空间
        </button>
      </header>
      <div class="panel-body">
        <div class="workspace-list">
          <button
            v-for="item in store.workspaces"
            :key="item.id"
            :class="{ active: item.id === store.currentWorkspace?.id }"
            @click="chooseWorkspace(item)"
          >
            <span
              ><strong>{{ item.name }}</strong
              ><small>{{
                item.owner_id === me.id ? "所有者" : "成员"
              }}</small></span
            ><Trash2
              v-if="item.owner_id === me.id"
              :size="14"
              @click.stop="deleteWorkspace(item)"
            />
          </button>
        </div>
      </div>
    </section>
    <section class="panel workspace-section">
      <header class="panel-header">
        <h3>成员权限</h3>
        <ShieldCheck :size="16" />
      </header>
      <div class="panel-body">
        <div v-if="canManage" class="heading-actions">
          <button class="button primary" @click="openInvite">
            <UserPlus :size="14" />邀请
          </button>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>账号</th>
              <th>角色</th>
              <th>权限</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in members" :key="item.user_id">
              <td>{{ item.username }}</td>
              <td>{{ item.is_owner ? "所有者" : "成员" }}</td>
              <td>
                <button
                  class="button small"
                  :disabled="item.is_owner || !canManage"
                  @click="changePermission(item)"
                >
                  {{ item.can_edit ? "可编辑" : "仅使用" }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
  <section v-if="me.is_admin" class="panel workspace-section">
    <header class="panel-header">
      <h3>平台账号</h3>
      <span>仅管理员</span>
    </header>
    <div class="panel-body">
      <button class="button primary" @click="userModal = true">
          <UserPlus :size="14" />创建子账号
      </button>
      <table class="data-table">
        <thead>
          <tr>
            <th>用户名</th>
            <th>类型</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in users" :key="item.id">
            <td>{{ item.username }}</td>
            <td>{{ item.is_admin ? "管理员" : "普通账号" }}</td>
            <td>{{ formatBeijingDateTime(item.created_at) }}</td>
            <td>
              <div class="table-actions">
                <button class="button small" @click="resetPassword(item)">
                  <KeyRound :size="13" />重置密码</button
                ><button
                  v-if="!item.is_admin"
                  class="icon-button"
                  title="删除账号"
                  @click="deleteUser(item)"
                >
                  <Trash2 :size="14" />
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
  <div v-if="workspaceModal" class="modal-backdrop" @click.self="workspaceModal=false"><section class="modal"><header class="modal-header"><div><span class="eyebrow">WORKSPACE</span><h2>新建工作空间</h2></div><button class="icon-button" @click="workspaceModal=false"><X :size="16" /></button></header><div class="modal-body"><div class="field"><label>工作空间名称</label><input v-model="workspaceName" autofocus placeholder="例如：产品研发组" @keyup.enter="createWorkspace" /></div></div><footer class="modal-footer"><button class="button" @click="workspaceModal=false">取消</button><button class="button primary" :disabled="!workspaceName.trim()" @click="createWorkspace"><Plus :size="14" />创建工作空间</button></footer></section></div>
  <div v-if="userModal" class="modal-backdrop" @click.self="userModal=false"><section class="modal"><header class="modal-header"><div><h2>创建用户</h2></div><button class="icon-button" @click="userModal=false"><X :size="16" /></button></header><div class="modal-body"><div class="form-grid"><div class="field full"><label>用户名</label><input v-model="newUsername" autofocus /></div><div class="field full"><label>初始密码</label><input v-model="newPassword" type="password" placeholder="至少 8 位" /></div></div></div><footer class="modal-footer"><button class="button" @click="userModal=false">取消</button><button class="button primary" :disabled="!newUsername.trim()||newPassword.length<8" @click="createUser">创建用户</button></footer></section></div>
  <div v-if="inviteModal" class="modal-backdrop" @click.self="inviteModal=false"><section class="modal"><header class="modal-header"><div><h2>邀请到工作空间</h2><small>{{ store.currentWorkspace?.name }}</small></div><button class="icon-button" @click="inviteModal=false"><X :size="16" /></button></header><div class="modal-body"><div class="field"><label>选择用户</label><select v-model="inviteName"><option value="" disabled>请选择用户</option><option v-for="item in inviteCandidates" :key="item.id" :value="item.username">{{ item.username }}</option></select></div><label class="toggle-row"><span>允许编辑工作空间内容</span><input v-model="inviteEdit" type="checkbox" class="toggle" /></label><p v-if="!inviteCandidates.length" class="muted">没有可邀请的用户。</p></div><footer class="modal-footer"><button class="button" @click="inviteModal=false">取消</button><button class="button primary" :disabled="!inviteName" @click="invite">发送邀请</button></footer></section></div>
</template>
