from odoo import models, fields, api, _, exceptions, tools
from odoo.exceptions import ValidationError, AccessError, UserError
from openai import OpenAI
import logging
import json
import pytz
from datetime import datetime, timedelta
import uuid
import re

_logger = logging.getLogger(__name__)

class AIUserSettings(models.Model):
    _name = 'ai.user.settings'
    _description = 'AI User Settings'
    _rec_name = 'user_id'
    
    user_id = fields.Many2one('res.users', string='User', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', string='Company', required=True, default=lambda self: self.env.company)
    
    # Daily usage limits
    daily_gpt4_limit = fields.Integer('Daily GPT-4 Limit', default=5)
    gpt4_usage_count = fields.Integer('GPT-4 Usage Today', default=0)
    last_reset_date = fields.Date('Last Reset Date', default=fields.Date.today)
    fallback_to_gpt35 = fields.Boolean('Fallback to GPT-3.5', default=True, 
                                      help="If enabled, requests will fall back to GPT-3.5 when GPT-4 limit is reached")
    
    # Model preferences
    default_model = fields.Selection([
        ('gpt-3.5-turbo', 'GPT-3.5 Turbo'),
        ('gpt-4', 'GPT-4'),
        ('gpt-4-turbo', 'GPT-4-turbo'),
        ('gpt-4o-mini', 'GPT-4o Mini'),
        ('gpt-4o', 'GPT-4o'),
    ], string='Default Model', default='gpt-3.5-turbo')
    
    # Generation settings
    temperature = fields.Float('Temperature', default=0.7, 
                             help="Higher values produce more random outputs")
    max_tokens = fields.Integer('Max Tokens', default=2000,
                              help="Maximum length of the generated response")
    
    # System prompt customization
    custom_system_prompt = fields.Text('Custom System Prompt',
                                     help="Additional instructions for the AI")
    
    # Analytics
    total_tokens_used = fields.Integer('Total Tokens Used', default=0)
    token_usage_this_month = fields.Integer('Tokens This Month', compute='_compute_token_usage_this_month')
    
    _sql_constraints = [
        ('user_company_unique', 'UNIQUE(user_id, company_id)', 'A user can only have one AI settings per company')
    ]
    
    def _compute_token_usage_this_month(self):
        """Compute token usage for the current month"""
        for record in self:
            today = fields.Date.today()
            start_of_month = today.replace(day=1)
            
            # Get all messages from this user this month
            messages = self.env['ai.chat.message'].search([
                ('create_date', '>=', start_of_month),
                ('chat_id.user_id', '=', record.user_id.id),
                ('token_count', '>', 0)
            ])
            
            record.token_usage_this_month = sum(messages.mapped('token_count'))
    
    @api.model
    def _cron_reset_daily_limits(self):
        """Cron job to reset daily limits for all users"""
        today = fields.Date.today()
        settings_to_reset = self.search([
            ('last_reset_date', '<', today)
        ])
        settings_to_reset.write({
            'gpt4_usage_count': 0,
            'last_reset_date': today
        })
        _logger.info(f"Reset GPT-4 limits for {len(settings_to_reset)} users")
    
    @api.model
    def get_user_settings(self, user_id=None):
        """Get or create settings for the given user"""
        if not user_id:
            user_id = self.env.user.id
        
        settings = self.search([
            ('user_id', '=', user_id),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
        
        if not settings:
            settings = self.create({
                'user_id': user_id,
                'company_id': self.env.company.id
            })
        
        # Check if we need to reset counter (if last_reset_date is not today)
        today = fields.Date.today()
        if settings.last_reset_date < today:
            settings.write({
                'gpt4_usage_count': 0,
                'last_reset_date': today
            })
        
        return settings
    
    def increment_gpt4_usage(self):
        """Increment GPT-4 usage counter and update last reset date if needed"""
        self.ensure_one()
        
        # Reset counter if needed
        today = fields.Date.today()
        if self.last_reset_date < today:
            self.gpt4_usage_count = 1
            self.last_reset_date = today
        else:
            self.gpt4_usage_count += 1
        
        return True
    
    def check_gpt4_limit(self):
        """Check if user has reached their GPT-4 limit"""
        self.ensure_one()
        
        # Reset counter if needed
        today = fields.Date.today()
        if self.last_reset_date < today:
            self.gpt4_usage_count = 0
            self.last_reset_date = today
            return True
        
        # Check if under limit
        return self.gpt4_usage_count < self.daily_gpt4_limit
    
class AIChatMessage(models.Model):
    _name = 'ai.chat.message'
    _description = 'AI Chat Message'
    _order = 'create_date asc'
    
    chat_id = fields.Many2one('ai.chat', string='Chat', required=True, ondelete='cascade')
    user_id = fields.Many2one('res.users', string='User', related='chat_id.user_id', store=True)
    message_type = fields.Selection([
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
    ], string='Message Type', required=True)
    content = fields.Text('Content', required=True)
    context_data = fields.Text('Context Data')
    model_used = fields.Char('Model Used')
    token_count = fields.Integer('Token Count')
    message_uuid = fields.Char('Message UUID', default=lambda self: str(uuid.uuid4()), readonly=True)
    response_time = fields.Float('Response Time (sec)')

class AIChat(models.Model):
    _name = 'ai.chat'
    _description = 'AI Chat Session'
    _order = 'last_message_date desc'
    _inherit = ['mail.thread', 'mail.activity.mixin']

    name = fields.Char('Chat Title', required=True, tracking=True)
    user_id = fields.Many2one('res.users', string='User', required=True, default=lambda self: self.env.user, 
                           readonly=True, tracking=True)
    message_ids = fields.One2many('ai.chat.message', 'chat_id', string='Messages')
    last_message_date = fields.Datetime('Last Message', compute='_compute_last_message', store=True)
    active = fields.Boolean(default=True, tracking=True)
    session_token = fields.Char(string='Session Token', default=lambda self: str(uuid.uuid4()), readonly=True)
    
    # For access control
    company_id = fields.Many2one('res.company', string='Company', required=True, 
                              default=lambda self: self.env.company)
    
    # Summary and context
    summary = fields.Text('Summary', compute='_compute_summary', store=True)
    topic = fields.Char('Topic', compute='_compute_topic', store=True)
    
    # Analytics
    total_tokens = fields.Integer('Total Tokens Used', compute='_compute_token_usage', store=True)
    total_messages = fields.Integer('Total Messages', compute='_compute_message_stats', store=True)
    avg_response_time = fields.Float('Avg Response Time (sec)', compute='_compute_response_time', store=True)

    category = fields.Selection([
        ('general', 'General'),
        ('business', 'Business'),
        ('support', 'Support'),
    ], string='Category', default='general', tracking=True)

    
    # Status and system information
    state = fields.Selection([
        ('active', 'Active'),
        ('archived', 'Archived'),
    ], string='Status', default='active', tracking=True)
    
    # Usage tracking
    gpt4_count = fields.Integer('GPT-4 Usage Count', default=0)
    gpt35_count = fields.Integer('GPT-3.5 Usage Count', default=0)
    
    @api.depends('message_ids.create_date')
    def _compute_last_message(self):
        for chat in self:
            last_message = chat.message_ids.sorted('create_date', reverse=True)[:1]
            chat.last_message_date = last_message.create_date if last_message else chat.create_date
    
    @api.depends('message_ids.token_count')
    def _compute_token_usage(self):
        for chat in self:
            chat.total_tokens = sum(chat.message_ids.mapped('token_count') or [0])
    
    @api.depends('message_ids')
    def _compute_message_stats(self):
        for chat in self:
            chat.total_messages = len(chat.message_ids)
    
    @api.depends('message_ids.response_time')
    def _compute_response_time(self):
        for chat in self:
            response_times = chat.message_ids.filtered('response_time').mapped('response_time')
            chat.avg_response_time = sum(response_times) / len(response_times) if response_times else 0
    
    @api.depends('message_ids.content', 'message_ids.message_type')
    def _compute_summary(self):
        """Generate a summary of the conversation using the AI itself"""
        for chat in self:
            if len(chat.message_ids) >= 5:  # Only summarize if we have enough messages
                try:
                    # Get the last few messages to summarize
                    messages = chat.message_ids.sorted('create_date', reverse=True)[:10]
                    messages = messages.sorted('create_date')
                    
                    # Prepare context for summarization
                    content_to_summarize = "\n".join([
                        f"{'User' if msg.message_type == 'user' else 'AI'}: {msg.content}" 
                        for msg in messages
                    ])
                    
                    # Call OpenAI for summarization
                    api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
                    if not api_key:
                        chat.summary = "API key not configured."
                        continue
                        
                    client = OpenAI(api_key=api_key)
                    response = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "Please provide a brief 1-2 sentence summary of this conversation."},
                            {"role": "user", "content": content_to_summarize}
                        ],
                        max_tokens=100
                    )
                    
                    chat.summary = response.choices[0].message.content.strip()
                except Exception as e:
                    _logger.error(f"Error generating summary: {str(e)}")
                    chat.summary = "Error generating summary."
            else:
                chat.summary = "Not enough messages to summarize."
    
    @api.depends('message_ids.content')
    def _compute_topic(self):
        """Extract the main topic from the first few messages"""
        for chat in self:
            if chat.message_ids:
                try:
                    # Get first few messages
                    messages = chat.message_ids.sorted('create_date')[:3]
                    
                    # Extract content
                    content = "\n".join([msg.content for msg in messages])
                    
                    # Call OpenAI to extract topic
                    api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
                    if not api_key:
                        chat.topic = "New Chat"
                        continue
                        
                    client = OpenAI(api_key=api_key)
                    response = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[
                            {"role": "system", "content": "Please provide a very brief 2-4 word topic for this conversation."},
                            {"role": "user", "content": content}
                        ],
                        max_tokens=20
                    )
                    
                    chat.topic = response.choices[0].message.content.strip()
                except Exception as e:
                    _logger.error(f"Error extracting topic: {str(e)}")
                    chat.topic = "New Chat"
            else:
                chat.topic = "New Chat"
    
    @api.model
    def create(self, vals):
        """Override create to set default name based on date if not provided"""
        if not vals.get('name'):
            vals['name'] = f"Chat {datetime.now().strftime('%d/%m/%Y %H:%M')}"
            # Nama akan diperbarui nanti oleh _compute_topic setelah pesan pertama dikirim
        return super(AIChat, self).create(vals)
    
    def archive_chat(self):
        """Archive chat instead of deleting it"""
        self.ensure_one()
        self.write({
            'active': False,
            'state': 'archived'
        })
        return True
    
    def restore_chat(self):
        """Restore archived chat"""
        self.ensure_one()
        self.write({
            'active': True,
            'state': 'active'
        })
        return True
    
    def clear_messages(self):
        """Clear all messages in the chat but keep the chat record"""
        self.ensure_one()
        self.message_ids.unlink()
        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def _get_employees_data(self, message):
        """Get employee attendance and performance data"""
        # Cek apakah query terkait karyawan
        message_lower = message.lower()
        employee_keywords = ['karyawan', 'absensi', 'kehadiran', 'service advisor', 'mechanic', 'mekanik', 'kinerja']
        
        if not any(keyword in message_lower for keyword in employee_keywords):
            return None
            
        result = ""
        
        # Analisis periode waktu dari pesan
        time_period = self._extract_time_period(message)
        date_from, date_to = self._get_date_range(time_period)
        
        # Cek apakah query tentang absensi
        if any(k in message_lower for k in ['absensi', 'kehadiran']):
            result += self._get_attendance_data(date_from, date_to, message)
        
        # Cek apakah query tentang kinerja mekanik
        if any(k in message_lower for k in ['mekanik', 'mechanic']):
            result += self._get_mechanic_performance(date_from, date_to, message)
        
        # Cek apakah query tentang kinerja service advisor
        if any(k in message_lower for k in ['service advisor', 'sa']):
            result += self._get_service_advisor_performance(date_from, date_to, message)
        
        # Cek apakah query tentang lead time servis
        if any(k in message_lower for k in ['lead time', 'durasi servis']):
            result += self._get_lead_time_analysis(date_from, date_to, message)
        
        return result

    def _get_attendance_data(self, date_from, date_to, message):
        """Get employee attendance data"""
        try:
            # Cari semua data absensi dalam rentang waktu
            attendance_data = self.env['hr.attendance'].search([
                ('check_in', '>=', date_from),
                ('check_in', '<=', date_to),
            ])
            
            if not attendance_data:
                return f"\n\nData Kehadiran ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan data kehadiran untuk periode waktu ini."
            
            # Analisis absensi
            total_attendance = len(attendance_data)
            employees_count = len(attendance_data.mapped('employee_id'))
            late_attendance = len(attendance_data.filtered('is_late'))
            late_percentage = (late_attendance / total_attendance) * 100 if total_attendance else 0
            
            # Kehadiran per departemen
            dept_data = {}
            for att in attendance_data:
                dept_name = att.employee_id.department_id.name or 'Tidak Terklasifikasi'
                if dept_name not in dept_data:
                    dept_data[dept_name] = {'count': 0, 'late': 0}
                dept_data[dept_name]['count'] += 1
                if att.is_late:
                    dept_data[dept_name]['late'] += 1
            
            # Format output
            result = f"\n\nData Kehadiran ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            result += f"- Total Kehadiran: {total_attendance}\n"
            result += f"- Jumlah Karyawan: {employees_count}\n"
            result += f"- Keterlambatan: {late_attendance} ({late_percentage:.2f}%)\n\n"
            
            # Detail per departemen
            result += "Kehadiran per Departemen:\n"
            for dept, data in dept_data.items():
                late_pct = (data['late'] / data['count']) * 100 if data['count'] else 0
                result += f"- {dept}: {data['count']} kehadiran, {data['late']} terlambat ({late_pct:.2f}%)\n"
            
            # Analisis karyawan sering terlambat (jika ada)
            if late_attendance > 0:
                late_employees = {}
                for att in attendance_data.filtered('is_late'):
                    emp_name = att.employee_id.name
                    late_employees[emp_name] = late_employees.get(emp_name, 0) + 1
                
                # Ambil 5 karyawan dengan keterlambatan tertinggi
                top_late = sorted(late_employees.items(), key=lambda x: x[1], reverse=True)[:5]
                
                if top_late:
                    result += "\nKaryawan dengan Keterlambatan Tertinggi:\n"
                    for emp_name, count in top_late:
                        result += f"- {emp_name}: {count} kali terlambat\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan data kehadiran: {str(e)}"
        
    def _get_mechanic_performance(self, date_from, date_to, message):
        """Get mechanic performance data"""
        try:
            # Cari semua order dengan service selesai dalam rentang waktu
            orders = self.env['sale.order'].search([
                ('controller_selesai', '>=', date_from),
                ('controller_selesai', '<=', date_to),
                ('car_mechanic_id_new', '!=', False)
            ])
            
            if not orders:
                return f"\n\nData Kinerja Mekanik ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan data service dengan mekanik untuk periode waktu ini."
            
            # Agregasi data berdasarkan mekanik
            mechanic_data = {}
            for order in orders:
                for mechanic in order.car_mechanic_id_new:
                    if mechanic.id not in mechanic_data:
                        mechanic_data[mechanic.id] = {
                            'name': mechanic.name,
                            'orders_count': 0,
                            'total_lead_time': 0,
                            'on_time_count': 0,
                            'service_efficiency': 0,
                            'customer_rating_sum': 0,
                            'customer_rating_count': 0
                        }
                    
                    mechanic_data[mechanic.id]['orders_count'] += 1
                    mechanic_data[mechanic.id]['total_lead_time'] += order.lead_time_servis
                    
                    if order.is_on_time:
                        mechanic_data[mechanic.id]['on_time_count'] += 1
                    
                    if order.service_time_efficiency:
                        mechanic_data[mechanic.id]['service_efficiency'] += order.service_time_efficiency
                    
                    if order.customer_rating:
                        mechanic_data[mechanic.id]['customer_rating_sum'] += int(order.customer_rating)
                        mechanic_data[mechanic.id]['customer_rating_count'] += 1
            
            # Hitung rata-rata dan persentase
            for mechanic_id in mechanic_data:
                data = mechanic_data[mechanic_id]
                orders_count = data['orders_count']
                
                # Hitung rata-rata lead time
                data['avg_lead_time'] = data['total_lead_time'] / orders_count if orders_count else 0
                
                # Hitung persentase on-time
                data['on_time_percentage'] = (data['on_time_count'] / orders_count) * 100 if orders_count else 0
                
                # Hitung rata-rata efisiensi
                data['avg_efficiency'] = data['service_efficiency'] / orders_count if orders_count else 0
                
                # Hitung rata-rata rating
                data['avg_rating'] = data['customer_rating_sum'] / data['customer_rating_count'] if data['customer_rating_count'] else 0
            
            # Format output
            result = f"\n\nData Kinerja Mekanik ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            result += f"Total order dengan mekanik: {len(orders)}\n\n"
            
            # Sortir mekanik berdasarkan jumlah order
            sorted_mechanics = sorted(mechanic_data.values(), key=lambda x: x['orders_count'], reverse=True)
            
            for mechanic in sorted_mechanics:
                result += f"Mekanik: {mechanic['name']}\n"
                result += f"- Total Order: {mechanic['orders_count']}\n"
                result += f"- Rata-rata Lead Time: {mechanic['avg_lead_time']:.2f} jam\n"
                result += f"- Persentase On-Time: {mechanic['on_time_percentage']:.2f}%\n"
                result += f"- Efisiensi Rata-rata: {mechanic['avg_efficiency']:.2f}%\n"
                
                if mechanic['customer_rating_count'] > 0:
                    result += f"- Rating Pelanggan: {mechanic['avg_rating']:.1f}/5 (dari {mechanic['customer_rating_count']} penilaian)\n"
                
                result += "\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan data kinerja mekanik: {str(e)}"
        
    def _get_service_advisor_performance(self, date_from, date_to, message):
        """Get service advisor performance data"""
        try:
            # Cari semua order dengan service selesai dalam rentang waktu
            orders = self.env['sale.order'].search([
                ('date_completed', '>=', date_from),
                ('date_completed', '<=', date_to),
                ('service_advisor_id', '!=', False)
            ])
            
            if not orders:
                return f"\n\nData Kinerja Service Advisor ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan data service dengan service advisor untuk periode waktu ini."
            
            # Agregasi data berdasarkan service advisor
            advisor_data = {}
            for order in orders:
                for advisor in order.service_advisor_id:
                    if advisor.id not in advisor_data:
                        advisor_data[advisor.id] = {
                            'name': advisor.name,
                            'orders_count': 0,
                            'total_revenue': 0,
                            'total_recommendations': 0,
                            'realized_recommendations': 0,
                            'customer_rating_sum': 0,
                            'customer_rating_count': 0
                        }
                    
                    advisor_data[advisor.id]['orders_count'] += 1
                    advisor_data[advisor.id]['total_revenue'] += order.amount_total
                    
                    # Rekomendasi
                    advisor_data[advisor.id]['total_recommendations'] += order.total_recommendations
                    advisor_data[advisor.id]['realized_recommendations'] += order.realized_recommendations
                    
                    # Rating
                    if order.customer_rating:
                        advisor_data[advisor.id]['customer_rating_sum'] += int(order.customer_rating)
                        advisor_data[advisor.id]['customer_rating_count'] += 1
            
            # Hitung rata-rata dan persentase
            for advisor_id in advisor_data:
                data = advisor_data[advisor_id]
                orders_count = data['orders_count']
                
                # Hitung rata-rata revenue
                data['avg_revenue'] = data['total_revenue'] / orders_count if orders_count else 0
                
                # Hitung persentase realisasi rekomendasi
                data['recommendation_realization'] = (data['realized_recommendations'] / data['total_recommendations']) * 100 if data['total_recommendations'] else 0
                
                # Hitung rata-rata rating
                data['avg_rating'] = data['customer_rating_sum'] / data['customer_rating_count'] if data['customer_rating_count'] else 0
            
            # Format output
            result = f"\n\nData Kinerja Service Advisor ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            result += f"Total order dengan service advisor: {len(orders)}\n\n"
            
            # Sortir advisor berdasarkan jumlah order
            sorted_advisors = sorted(advisor_data.values(), key=lambda x: x['orders_count'], reverse=True)
            
            for advisor in sorted_advisors:
                result += f"Service Advisor: {advisor['name']}\n"
                result += f"- Total Order: {advisor['orders_count']}\n"
                result += f"- Total Pendapatan: {advisor['total_revenue']:,.2f}\n"
                result += f"- Rata-rata Pendapatan: {advisor['avg_revenue']:,.2f} per order\n"
                
                if advisor['total_recommendations'] > 0:
                    result += f"- Rekomendasi: {advisor['realized_recommendations']} dari {advisor['total_recommendations']} ({advisor['recommendation_realization']:.2f}%)\n"
                
                if advisor['customer_rating_count'] > 0:
                    result += f"- Rating Pelanggan: {advisor['avg_rating']:.1f}/5 (dari {advisor['customer_rating_count']} penilaian)\n"
                
                result += "\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan data kinerja service advisor: {str(e)}"
        
    def _get_lead_time_analysis(self, date_from, date_to, message):
        """Get service lead time analysis"""
        try:
            # Cari semua order dengan service selesai dalam rentang waktu
            orders = self.env['sale.order'].search([
                ('controller_selesai', '>=', date_from),
                ('controller_selesai', '<=', date_to),
            ])
            
            if not orders:
                return f"\n\nAnalisis Lead Time Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan data service untuk periode waktu ini."
            
            # Agregasi data lead time
            total_orders = len(orders)
            total_lead_time = sum(orders.mapped('lead_time_servis'))
            avg_lead_time = total_lead_time / total_orders if total_orders else 0
            
            # Analisis berdasarkan kategori servis
            service_categories = {}
            for order in orders:
                category = order.service_category or 'Tidak Terklasifikasi'
                subcategory = order.service_subcategory or 'Tidak Terklasifikasi'
                
                if category not in service_categories:
                    service_categories[category] = {
                        'count': 0,
                        'total_lead_time': 0,
                        'subcategories': {}
                    }
                
                service_categories[category]['count'] += 1
                service_categories[category]['total_lead_time'] += order.lead_time_servis
                
                if subcategory not in service_categories[category]['subcategories']:
                    service_categories[category]['subcategories'][subcategory] = {
                        'count': 0,
                        'total_lead_time': 0
                    }
                
                service_categories[category]['subcategories'][subcategory]['count'] += 1
                service_categories[category]['subcategories'][subcategory]['total_lead_time'] += order.lead_time_servis
            
            # Analisis waktu tunggu
            total_wait_confirmation = sum(orders.mapped('lead_time_tunggu_konfirmasi') or [0])
            total_wait_part1 = sum(orders.mapped('lead_time_tunggu_part1') or [0])
            total_wait_part2 = sum(orders.mapped('lead_time_tunggu_part2') or [0])
            total_wait_sublet = sum(orders.mapped('lead_time_tunggu_sublet') or [0])
            
            avg_wait_confirmation = total_wait_confirmation / total_orders if total_orders else 0
            avg_wait_part1 = total_wait_part1 / total_orders if total_orders else 0
            avg_wait_part2 = total_wait_part2 / total_orders if total_orders else 0
            avg_wait_sublet = total_wait_sublet / total_orders if total_orders else 0
            
            # Format output
            result = f"\n\nAnalisis Lead Time Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            result += f"- Total Order: {total_orders}\n"
            result += f"- Rata-rata Lead Time: {avg_lead_time:.2f} jam\n\n"
            
            # Lead time berdasarkan kategori
            result += "Lead Time berdasarkan Kategori Servis:\n"
            for category, data in service_categories.items():
                avg_category_lead_time = data['total_lead_time'] / data['count'] if data['count'] else 0
                result += f"- {category}: {avg_category_lead_time:.2f} jam (dari {data['count']} order)\n"
                
                # Detail subcategory jika relevan dengan query
                if 'detail' in message.lower() or 'rinci' in message.lower():
                    for subcategory, subdata in data['subcategories'].items():
                        avg_subcategory_lead_time = subdata['total_lead_time'] / subdata['count'] if subdata['count'] else 0
                        result += f"  * {subcategory}: {avg_subcategory_lead_time:.2f} jam (dari {subdata['count']} order)\n"
            
            result += "\nAnalisis Waktu Tunggu:\n"
            result += f"- Rata-rata Tunggu Konfirmasi: {avg_wait_confirmation:.2f} jam\n"
            result += f"- Rata-rata Tunggu Part 1: {avg_wait_part1:.2f} jam\n"
            result += f"- Rata-rata Tunggu Part 2: {avg_wait_part2:.2f} jam\n"
            result += f"- Rata-rata Tunggu Sublet: {avg_wait_sublet:.2f} jam\n"
            
            # Analisis efficiency
            orders_with_efficiency = orders.filtered(lambda o: o.service_time_efficiency > 0)
            if orders_with_efficiency:
                avg_efficiency = sum(orders_with_efficiency.mapped('service_time_efficiency')) / len(orders_with_efficiency)
                result += f"\nRata-rata Efisiensi Waktu Servis: {avg_efficiency:.2f}%\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan analisis lead time: {str(e)}"
        
    def _get_performance_summary(self, date_from, date_to):
        """Get performance summary for all key metrics"""
        try:
            # Key metrics
            metrics = {
                'orders_completed': 0,
                'total_revenue': 0,
                'avg_lead_time': 0,
                'avg_customer_rating': 0,
                'top_service_advisors': [],
                'top_mechanics': [],
                'attendance_rate': 0
            }
            
            # Get completed orders
            orders = self.env['sale.order'].search([
                ('date_completed', '>=', date_from),
                ('date_completed', '<=', date_to),
                ('state', '=', 'done')
            ])
            
            metrics['orders_completed'] = len(orders)
            metrics['total_revenue'] = sum(orders.mapped('amount_total'))
            
            # Lead time
            orders_with_lead_time = orders.filtered('lead_time_servis')
            if orders_with_lead_time:
                metrics['avg_lead_time'] = sum(orders_with_lead_time.mapped('lead_time_servis')) / len(orders_with_lead_time)
            
            # Customer rating
            rated_orders = orders.filtered('customer_rating')
            if rated_orders:
                total_rating = sum(int(order.customer_rating) for order in rated_orders)
                metrics['avg_customer_rating'] = total_rating / len(rated_orders)
            
            # Top service advisors
            sa_performance = self.env['sale.order'].read_group(
                [('date_completed', '>=', date_from), ('date_completed', '<=', date_to)],
                ['service_advisor_id', 'amount_total:sum', 'customer_rating:avg', 'id:count'],
                ['service_advisor_id']
            )
            
            metrics['top_service_advisors'] = sorted(
                [p for p in sa_performance if p['service_advisor_id']],
                key=lambda p: p['amount_total'], 
                reverse=True
            )[:5]
            
            # Top mechanics
            mech_performance = self.env['sale.order'].read_group(
                [('controller_selesai', '>=', date_from), ('controller_selesai', '<=', date_to)],
                ['car_mechanic_id_new', 'lead_time_servis:avg', 'id:count'],
                ['car_mechanic_id_new']
            )
            
            metrics['top_mechanics'] = sorted(
                [p for p in mech_performance if p['car_mechanic_id_new']],
                key=lambda p: p['lead_time_servis'], 
            )[:5]
            
            # Attendance rate
            working_days = self._count_working_days(date_from, date_to)
            employees = self.env['hr.employee'].search([('active', '=', True)])
            
            attendance_data = self.env['hr.attendance'].read_group(
                [('check_in', '>=', date_from), ('check_in', '<=', date_to)],
                ['employee_id', 'id:count'],
                ['employee_id']
            )
            
            attendance_dict = {a['employee_id'][0]: a['id'] for a in attendance_data if a['employee_id']}
            total_expected = len(employees) * working_days
            total_actual = sum(attendance_dict.values())
            
            metrics['attendance_rate'] = (total_actual / total_expected * 100) if total_expected else 0
            
            return metrics
            
        except Exception as e:
            _logger.error(f"Error getting performance summary: {str(e)}")
            return {}

    def _count_working_days(self, date_from, date_to):
        """Count working days (Mon-Sat) between two dates"""
        days = 0
        current = date_from
        while current <= date_to:
            # If not Sunday (weekday 6)
            if current.weekday() != 6:
                days += 1
            current += timedelta(days=1)
        return days
    
    def _get_hr_data(self, message):
        """Get HR data including employee performance and attendance"""
        try:
            # Analisis periode waktu dari pesan
            time_period = self._extract_time_period(message)
            date_from, date_to = self._get_date_range(time_period)
            
            # Cek apakah ada kata kunci khusus
            message_lower = message.lower()
            is_attendance_query = any(k in message_lower for k in ['absensi', 'kehadiran', 'hadir'])
            is_performance_query = any(k in message_lower for k in ['kinerja', 'performance', 'rating'])
            
            result = f"\nData HR ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            
            # Jika query tentang kehadiran
            if is_attendance_query:
                # Ambil data kehadiran seluruh perusahaan
                attendance_data = self.env['hr.attendance'].search([
                    ('check_in', '>=', date_from),
                    ('check_in', '<=', date_to),
                ])
                
                # Hitung metrik
                total_attendances = len(attendance_data)
                unique_employees = len(attendance_data.mapped('employee_id'))
                late_attendances = len(attendance_data.filtered('is_late'))
                
                result += f"- Total Kehadiran: {total_attendances}\n"
                result += f"- Karyawan Unik: {unique_employees}\n"
                result += f"- Keterlambatan: {late_attendances} ({late_attendances/total_attendances*100:.2f}% dari total)\n\n"
                
                # Analisis per departemen
                dept_data = self.env['hr.attendance'].read_group(
                    [('check_in', '>=', date_from), ('check_in', '<=', date_to)],
                    ['employee_id.department_id', 'id:count', 'is_late:sum'],
                    ['employee_id.department_id']
                )
                
                result += "Kehadiran per Departemen:\n"
                for dept in dept_data:
                    dept_name = dept['employee_id.department_id'][1] if dept['employee_id.department_id'] else 'Tidak Ada Departemen'
                    count = dept['id']
                    late = dept['is_late']
                    result += f"- {dept_name}: {count} kehadiran, {late} keterlambatan ({late/count*100:.2f}%)\n"
            
            # Jika query tentang kinerja
            if is_performance_query:
                # Ambil metrik kinerja untuk service advisor
                if 'service advisor' in message_lower or 'sa' in message_lower:
                    sa_metrics = self.env['sale.order'].read_group(
                        [('date_completed', '>=', date_from), ('date_completed', '<=', date_to), ('service_advisor_id', '!=', False)],
                        ['service_advisor_id', 'amount_total:sum', 'customer_rating:avg', 'id:count'],
                        ['service_advisor_id']
                    )
                    
                    if sa_metrics:
                        result += "\nKinerja Service Advisor:\n"
                        sorted_sa = sorted(sa_metrics, key=lambda x: x['amount_total'], reverse=True)
                        
                        for sa in sorted_sa:
                            if not sa['service_advisor_id']:
                                continue
                            name = sa['service_advisor_id'][1]
                            orders = sa['id']
                            revenue = sa['amount_total']
                            rating = sa['customer_rating'] or 0
                            
                            result += f"- {name}: {orders} order, Pendapatan: {revenue:,.2f}, Rating: {rating:.1f}/5\n"
                
                # Ambil metrik kinerja untuk mekanik
                if 'mekanik' in message_lower or 'mechanic' in message_lower:
                    mechanic_metrics = self.env['sale.order'].read_group(
                        [('controller_selesai', '>=', date_from), ('controller_selesai', '<=', date_to), ('car_mechanic_id_new', '!=', False)],
                        ['car_mechanic_id_new', 'lead_time_servis:avg', 'service_time_efficiency:avg', 'id:count'],
                        ['car_mechanic_id_new']
                    )
                    
                    if mechanic_metrics:
                        result += "\nKinerja Mekanik:\n"
                        sorted_mech = sorted(mechanic_metrics, key=lambda x: x.get('service_time_efficiency', 0), reverse=True)
                        
                        for mech in sorted_mech:
                            if not mech['car_mechanic_id_new']:
                                continue
                            name = mech['car_mechanic_id_new'][1]
                            orders = mech['id']
                            lead_time = mech['lead_time_servis'] or 0
                            efficiency = mech['service_time_efficiency'] or 0
                            
                            result += f"- {name}: {orders} order, Lead Time: {lead_time:.2f} jam, Efisiensi: {efficiency:.2f}%\n"
            
            # Tambahkan saran perbaikan (jika tersedia)
            if total_attendances > 0 and late_attendances / total_attendances > 0.1:
                result += "\nSaran Perbaikan:\n"
                result += "- Tingkat keterlambatan lebih dari 10%, perlu evaluasi ketepatan waktu karyawan\n"
            
            return result
        
        except Exception as e:
            _logger.error(f"Error getting HR data: {str(e)}")
            return f"\nError mendapatkan data HR: {str(e)}"
        
    def _get_comprehensive_data(self, message):
        """Get comprehensive business data covering multiple aspects"""
        try:
            # Analisis periode waktu dari pesan
            time_period = self._extract_time_period(message)
            date_from, date_to = self._get_date_range(time_period)
            
            result = f"\nLAPORAN KOMPREHENSIF BISNIS ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')})\n"
            result += "=" * 80 + "\n\n"
            
            # 1. RINGKASAN EKSEKUTIF
            metrics = self._get_performance_summary(date_from, date_to)
            
            result += "RINGKASAN EKSEKUTIF:\n"
            result += f"- Order Selesai: {metrics.get('orders_completed', 0)}\n"
            result += f"- Total Pendapatan: {metrics.get('total_revenue', 0):,.2f}\n"
            result += f"- Rata-rata Lead Time: {metrics.get('avg_lead_time', 0):.2f} jam\n"
            result += f"- Rata-rata Rating Pelanggan: {metrics.get('avg_customer_rating', 0):.1f}/5\n"
            result += f"- Tingkat Kehadiran Karyawan: {metrics.get('attendance_rate', 0):.2f}%\n\n"
            
            # 2. ANALISIS PERFORMA SALES
            sales_data = self._get_sales_data(date_from, date_to, message)
            result += "PERFORMA SALES:\n"
            result += sales_data + "\n\n"
            
            # 3. PERFORMA SERVICE
            service_data = self._get_lead_time_analysis(date_from, date_to, message)
            result += "PERFORMA SERVICE:\n"
            result += service_data + "\n\n"
            
            # 4. KINERJA KARYAWAN
            hr_data = self._get_hr_data(message)
            result += "KINERJA KARYAWAN:\n"
            result += hr_data + "\n\n"
            
            # 5. METRIK KUALITAS
            sa_performance = self._get_service_advisor_performance(date_from, date_to, message)
            mechanic_performance = self._get_mechanic_performance(date_from, date_to, message)
            
            result += "METRIK KUALITAS:\n"
            result += "- Service Advisor Performance:\n"
            result += sa_performance + "\n"
            result += "- Mechanic Performance:\n"
            result += mechanic_performance + "\n\n"
            
            # 6. REKOMENDASI & INSIGHT
            result += "REKOMENDASI & INSIGHT:\n"
            
            # Analyze underperforming areas
            if metrics.get('attendance_rate', 0) < 90:
                result += "- Tingkat kehadiran di bawah 90%, perlu ditingkatkan kedisiplinan karyawan\n"
            
            if metrics.get('avg_customer_rating', 0) < 4:
                result += "- Rating pelanggan di bawah 4/5, perlu peningkatan kualitas layanan\n"
            
            # Add business growth suggestions
            result += "- Pertimbangkan program loyalitas untuk pelanggan dengan frekuensi servis tinggi\n"
            result += "- Optimalkan jadwal mekanik berdasarkan performa lead time\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error getting comprehensive data: {str(e)}")
            return f"\nError mendapatkan data komprehensif: {str(e)}"
    
    def send_message(self, content, model=None, query_mode='auto'):
        """Send a message in this chat and get AI response"""
        self.ensure_one()
        
        if not content.strip():
            return {'error': 'Message cannot be empty'}
        
        # Check user access to this chat
        if self.user_id.id != self.env.user.id:
            return {'error': 'You do not have access to this chat'}
        
        # Get system settings
        api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
        if not api_key:
            return {'error': 'OpenAI API key not configured'}
            
        # Get user settings
        user_settings = self.env['ai.user.settings'].get_user_settings()
        
        # Determine which model to use
        model_to_use = model or user_settings.default_model
        
        # If GPT-4 is requested, check daily limit
        if model_to_use.startswith('gpt-4'):
            if not user_settings.check_gpt4_limit():
                # If user has reached their limit, fallback or return error
                if user_settings.fallback_to_gpt35:
                    model_to_use = 'gpt-3.5-turbo'
                else:
                    return {
                        'error': f'You have reached your daily limit of {user_settings.daily_gpt4_limit} GPT-4 requests',
                        'remaining_tokens': 0
                    }
        
        # Create user message
        message_time = fields.Datetime.now()
        user_message = self.env['ai.chat.message'].create({
            'chat_id': self.id,
            'message_type': 'user',
            'content': content,
            'create_date': message_time,
        })

        is_first_message = len(self.message_ids) <= 1
        
        try:
             # Deteksi jenis pertanyaan
            is_business_query = self._is_business_query(content)
            if query_mode == 'auto':
                is_business_query = self._is_business_query(content)
            elif query_mode == 'general':
                is_business_query = False
            
            # Analyze message to gather relevant data from Odoo (hanya jika bisnis)
            context_data = None
            if is_business_query:
                # Tambahkan pengecekan untuk pertanyaan tentang karyawan
                if any(k in content.lower() for k in ['karyawan', 'absensi', 'service advisor', 'mechanic', 'mekanik']):
                    context_data = self._gather_employee_data(content)
                else:
                    context_data = self._gather_relevant_data(content)
            
            # Get system prompt
            system_prompt = self._get_system_prompt(user_settings, is_business_query)
            
            # Get chat history for context (last 10 messages)
            chat_history = self.message_ids.sorted('create_date', reverse=True)[1:10]
            chat_history = chat_history.sorted('create_date')
            
            # Prepare messages for API
            messages = [{"role": "system", "content": system_prompt}]
            
            # Add chat history
            for msg in chat_history:
                role = "user" if msg.message_type == "user" else "assistant"
                messages.append({"role": role, "content": msg.content})
            
            # Add current message with context data jika query bisnis
            current_msg_content = content
            if is_business_query and context_data:
                current_msg_content += f"\n\n[SYSTEM: Here is relevant data from the Odoo ERP system to help answer this question]\n{context_data}"
            
            messages.append({"role": "user", "content": current_msg_content})
            
            # Call OpenAI API
            client = OpenAI(api_key=api_key)
            response_time_start = datetime.now()
            
            response = client.chat.completions.create(
                model=model_to_use,
                messages=messages,
                temperature=user_settings.temperature or 0.7,
                max_tokens=user_settings.max_tokens or 2000
            )
            
            response_time = (datetime.now() - response_time_start).total_seconds()
            
            # Update message counts
            if model_to_use.startswith('gpt-4'):
                self.gpt4_count += 1
                user_settings.increment_gpt4_usage()
            else:
                self.gpt35_count += 1
            
            # Save AI response
            ai_response = self.env['ai.chat.message'].create({
                'chat_id': self.id,
                'message_type': 'assistant',
                'content': response.choices[0].message.content,
                'context_data': context_data if context_data else None,
                'model_used': model_to_use,
                'token_count': response.usage.total_tokens,
                'response_time': response_time,
                'create_date': fields.Datetime.now(),
            })

            if is_first_message:
                # Jika ini pesan pertama, picu komputasi topic
                self._update_chat_name_from_first_message(content)
            
            # Return success with response data
            return {
                'success': True,
                'response': {
                    'content': ai_response.content,
                    'model_used': model_to_use,
                    'token_count': response.usage.total_tokens,
                    'id': ai_response.id,
                    'message_id': ai_response.message_uuid
                }
            }
            
        except Exception as e:
            _logger.error(f"OpenAI API Error: {str(e)}")
            
            # Create error message
            error_msg = f"Sorry, I encountered an error while processing your request: {str(e)}"
            self.env['ai.chat.message'].create({
                'chat_id': self.id,
                'message_type': 'system',
                'content': error_msg,
                'model_used': model_to_use,
                'create_date': fields.Datetime.now(),
            })
            
            return {'error': str(e)}
        
    def _update_chat_name(self, user_message, ai_response):
        """Create a descriptive chat name from first interaction"""
        try:
            # Gunakan API yang sama yang sudah merespon untuk menghemat token
            api_key = self.env['ir.config_parameter'].sudo().get_param('openai.api_key')
            if not api_key:
                return
                
            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Based on this first exchange in a chat, please create a very short, descriptive title (2-5 words). Just return the title, nothing else."},
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": ai_response}
                ],
                max_tokens=10
            )
            
            new_name = response.choices[0].message.content.strip()
            if new_name:
                # Hapus tanda kutip jika ada
                new_name = new_name.strip('"\'')
                self.write({'name': new_name})
        except Exception as e:
            _logger.error(f"Error updating chat name: {str(e)}")

    def _is_business_query(self, message):
        """Determine if a message is business-related or general knowledge"""
        business_keywords = [
            'sales', 'revenue', 'inventory', 'stock', 'finance', 'invoice', 
            'customer', 'product', 'order', 'purchase', 'company', 'business',
            'profit', 'expense', 'accounting', 'balance', 'vendor', 'employee',
            'penjualan', 'persediaan', 'stok', 'keuangan', 'faktur', 'pelanggan',
            'produk', 'pesanan', 'pembelian', 'perusahaan', 'bisnis', 'keuntungan',
            'biaya', 'akuntansi', 'saldo', 'vendor', 'karyawan',
            # Kata kunci baru untuk karyawan dan kinerja
            'attendance', 'hadir', 'absensi', 'kehadiran', 'performance', 'kinerja',
            'service advisor', 'mechanic', 'mekanik', 'lead time', 'durasi', 'rating'
        ]
        
        message_lower = message.lower()
        
        # Cek jika ada kata kunci bisnis
        for keyword in business_keywords:
            if keyword in message_lower:
                return True
        
        # Jika tidak ada kata kunci bisnis, cek jika pertanyaan tentang data perusahaan
        data_indicators = ['how many', 'how much', 'berapa', 'total', 'average', 'rata-rata', 
                        'performance', 'kinerja', 'compare', 'bandingkan', 'analyze', 
                        'analisis', 'report', 'laporan']
        
        for indicator in data_indicators:
            if indicator in message_lower:
                return True
        
        # Jika tidak ada indikator bisnis, kemungkinan pertanyaan umum
        return False

    
    def _get_system_prompt(self, user_settings, is_business_query=True):
        """Get system prompt combining default and user customizations"""
        if is_business_query:
            base_prompt = """
You are an AI assistant integrated with Odoo ERP. 
You help users analyze business data and make informed decisions.
Below are some guidelines to follow:

1. When analyzing data, provide clear insights and actionable recommendations
2. Answer based only on the data provided, avoid making assumptions
3. For sales analysis, compare performance across time periods and calculate growth rates
4. For inventory analysis, identify low stock items and potential ordering needs
5. For finance analysis, highlight important metrics and trends
6. Use formatting to make your responses easy to read
7. If you don't have the data to answer a question, explain what data would be needed

The data in square brackets [DATA] is provided by the system - it's not visible to the user but provides you the context to answer their questions accurately.
"""
        else:
            base_prompt = """
You are an AI assistant that can help with general knowledge questions.
Your name is AI Business Assistant, but you can answer questions on a wide range of topics beyond just business.

When answering questions:
1. Be helpful, accurate, and informative
2. Use your knowledge about the world, science, technology, history, etc.
3. If you're not confident in an answer, say so
4. Format your responses for easy reading
5. If appropriate, provide examples or analogies to explain complex topics
"""

        # Add company information for business queries
        if is_business_query:
            company_info = self._get_company_info()
            if company_info:
                base_prompt += f"\n\nCompany Information:\n{company_info}\n"
        
        # Add user customization if any
        if user_settings.custom_system_prompt:
            base_prompt += f"\n\nAdditional Guidelines:\n{user_settings.custom_system_prompt}"
        
        return base_prompt
    
    def _get_company_info(self):
        """Get basic company information"""
        company = self.company_id or self.env.company
        return f"""
- Name: {company.name}
- Website: {company.website or 'Not set'}
- Email: {company.email or 'Not set'}
- Phone: {company.phone or 'Not set'}
"""
    
    def _gather_relevant_data(self, message):
        """Analyze message and gather relevant data from Odoo, including HR data"""
        message_lower = message.lower()
        
        # Cek apakah pertanyaan meminta laporan komprehensif
        if any(k in message_lower for k in ['komprehensif', 'lengkap', 'menyeluruh', 'comprehensive']):
            return self._get_comprehensive_data(message)
        
        # Analyze message untuk menentukan kategori data
        data_categories = self._analyze_message(message)
        
        result = []
        
        # Get data untuk setiap kategori
        for category in data_categories:
            method_name = f"_get_{category}_data"
            if hasattr(self, method_name):
                try:
                    category_data = getattr(self, method_name)(message)
                    if category_data:
                        result.append(category_data)
                except Exception as e:
                    _logger.error(f"Error gathering {category} data: {str(e)}")
        
        # Cek untuk data HR dan attendance
        if any(k in message_lower for k in ['karyawan', 'employee', 'absensi', 'attendance', 'hadir']):
            hr_data = self._get_hr_data(message)
            if hr_data:
                result.append(hr_data)
        
        # Cek untuk data kinerja service advisor
        if any(k in message_lower for k in ['service advisor', 'sa', 'advisor']):
            sa_data = self._get_service_advisor_performance(None, None, message)
            if sa_data:
                result.append(sa_data)
        
        # Cek untuk data kinerja mekanik
        if any(k in message_lower for k in ['mechanic', 'mekanik']):
            mechanic_data = self._get_mechanic_performance(None, None, message)
            if mechanic_data:
                result.append(mechanic_data)
        
        # Cek untuk data lead time
        if any(k in message_lower for k in ['lead time', 'durasi', 'waktu']):
            lead_time_data = self._get_lead_time_analysis(None, None, message)
            if lead_time_data:
                result.append(lead_time_data)
        
        # Join semua data dengan separator
        if result:
            return "\n\n---\n\n".join(result)
        else:
            # Default basic company data if no specific data found
            return self._get_basic_company_data()
    
    def _analyze_message(self, message):
        """Analyze the message to determine what data to fetch"""
        message_lower = message.lower()
        
        # Define categories with keywords
        categories = {
            'sales': ['sales', 'revenue', 'customer', 'order', 'client', 'income', 'penjualan', 'pelanggan', 'pendapatan', 'pesanan'],
            'inventory': ['inventory', 'stock', 'product', 'warehouse', 'item', 'persediaan', 'stok', 'produk', 'gudang', 'barang'],
            'finance': ['invoice', 'payment', 'profit', 'loss', 'accounting', 'balance', 'faktur', 'pembayaran', 'keuntungan', 'kerugian', 'akuntansi', 'saldo'],
            'employees': ['employee', 'staff', 'hr', 'attendance', 'absensi', 'karyawan', 'pegawai', 'sdm', 'hadir', 'kehadiran'],
            'purchases': ['purchase', 'vendor', 'supplier', 'pembelian', 'vendor', 'pemasok'],
            'service': ['service', 'advisor', 'mechanic', 'mekanik', 'servis', 'lead time', 'durasi', 'sa', 'sparepart', 'part'],
        }
        
        # Determine which categories match the message
        matched_categories = []
        for category, keywords in categories.items():
            if any(keyword in message_lower for keyword in keywords):
                matched_categories.append(category)
        
        # If no categories matched, return default
        if not matched_categories:
            return ['basic']
        
        return matched_categories
    
    def _get_attendance_records(self, employee_id, date_from, date_to):
        """Get attendance records for a specific employee and date range"""
        attendances = self.env['hr.attendance'].search([
            ('employee_id', '=', employee_id),
            ('check_in', '>=', date_from),
            ('check_in', '<=', date_to)
        ], order='check_in desc')
        
        return attendances

    def _get_employee_performance(self, employee_id, date_from, date_to):
        """Get employee performance metrics for a specific time period"""
        employee = self.env['hr.employee'].browse(employee_id)
        
        # Metrics dictionary
        metrics = {
            'name': employee.name,
            'department': employee.department_id.name,
            'attendance_count': 0,
            'late_count': 0,
            'orders_assigned': 0,
            'revenue_generated': 0,
            'avg_customer_rating': 0,
            'avg_lead_time': 0
        }
        
        # Get attendance data
        attendances = self._get_attendance_records(employee_id, date_from, date_to)
        metrics['attendance_count'] = len(attendances)
        metrics['late_count'] = len(attendances.filtered('is_late'))
        
        # Kinerja berdasarkan peran
        if employee.pitcar_role == 'service_advisor':
            # Untuk SA, cari order yang ditangani
            orders = self.env['sale.order'].search([
                ('service_advisor_id', 'in', [employee_id]),
                ('date_completed', '>=', date_from),
                ('date_completed', '<=', date_to)
            ])
            
            metrics['orders_assigned'] = len(orders)
            metrics['revenue_generated'] = sum(orders.mapped('amount_total'))
            
            # Rating dan kinerja lainnya
            rated_orders = orders.filtered('customer_rating')
            if rated_orders:
                ratings_sum = sum(int(order.customer_rating) for order in rated_orders)
                metrics['avg_customer_rating'] = ratings_sum / len(rated_orders)
        
        elif employee.pitcar_role == 'mechanic':
            # Untuk mekanik, cari order dimana mereka ditugaskan
            orders = self.env['sale.order'].search([
                '|',
                ('car_mechanic_id', '=', employee_id),
                ('car_mechanic_id_new', 'in', [employee_id]),
                ('controller_selesai', '>=', date_from),
                ('controller_selesai', '<=', date_to)
            ])
            
            metrics['orders_assigned'] = len(orders)
            
            # Lead time rata-rata
            lead_times = orders.mapped('lead_time_servis')
            if lead_times:
                metrics['avg_lead_time'] = sum(lead_times) / len(lead_times)
        
        return metrics
    
    def _get_basic_company_data(self):
        """Get basic company data as default"""
        company = self.company_id or self.env.company
        
        # Get some basic stats
        total_users = self.env['res.users'].search_count([('company_id', '=', company.id)])
        total_customers = self.env['res.partner'].search_count([
            ('company_id', '=', company.id),
            ('customer_rank', '>', 0)
        ])
        
        return f"""
Basic Company Information:
- Name: {company.name}
- Website: {company.website or 'Not set'}
- Email: {company.email or 'Not set'}
- Phone: {company.phone or 'Not set'}
- Address: {company.street or ''} {company.city or ''}, {company.country_id.name or ''}
- Total users: {total_users}
- Total customers: {total_customers}
"""
    
    def _get_sales_data(self, message):
        # Determine time period from message
        time_period = self._extract_time_period(message)
        
        # Get date range for the period
        date_from, date_to = self._get_date_range(time_period)
        
        # Make sure these dates are being used properly in the domain
        domain = [
            ('state', 'in', ['sale', 'done']),
            ('date_order', '>=', date_from),
            ('date_order', '<=', date_to),
            ('company_id', '=', self.company_id.id)
        ]
        
        # Log the actual date range being used (for debugging)
        _logger.info(f"Querying sales from {date_from} to {date_to} based on message: {message}")

        
        orders = self.env['sale.order'].search(domain)
        
        if not orders:
            return f"No sales data found for the period {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}."
        
        # Basic metrics
        total_orders = len(orders)
        total_amount = sum(orders.mapped('amount_total'))
        avg_order_value = total_amount / total_orders if total_orders else 0
        
        # Group by customer
        customer_data = {}
        for order in orders:
            customer_name = order.partner_id.name
            if customer_name in customer_data:
                customer_data[customer_name]['order_count'] += 1
                customer_data[customer_name]['total_amount'] += order.amount_total
            else:
                customer_data[customer_name] = {
                    'order_count': 1,
                    'total_amount': order.amount_total
                }
        
        # Get top customers
        top_customers = sorted(
            customer_data.items(), 
            key=lambda x: x[1]['total_amount'], 
            reverse=True
        )[:5]
        
        # Get product sales
        product_data = {}
        for order in orders:
            for line in order.order_line:
                product_name = line.product_id.name
                if product_name in product_data:
                    product_data[product_name]['qty'] += line.product_uom_qty
                    product_data[product_name]['amount'] += line.price_subtotal
                else:
                    product_data[product_name] = {
                        'qty': line.product_uom_qty,
                        'amount': line.price_subtotal
                    }
        
        # Get top products
        top_products = sorted(
            product_data.items(),
            key=lambda x: x[1]['amount'],
            reverse=True
        )[:5]
        
        # Format data
        result = f"""
Sales Data ({time_period}):
- Date Range: {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}
- Total Orders: {total_orders}
- Total Revenue: {total_amount:.2f}
- Average Order Value: {avg_order_value:.2f}

Top Customers:
"""
        for i, (customer, data) in enumerate(top_customers, 1):
            result += f"{i}. {customer}: {data['order_count']} orders, {data['total_amount']:.2f}\n"
        
        result += "\nTop Products:"
        for i, (product, data) in enumerate(top_products, 1):
            result += f"\n{i}. {product}: {data['qty']} units, {data['amount']:.2f}"
        
        return result
    
    def _get_inventory_data(self, message):
        """Get inventory data based on the user's message"""
        # Get products with low stock
        low_stock_domain = [
            ('type', '=', 'product'),
            ('qty_available', '<', 10),
            ('company_id', '=', self.company_id.id)
        ]
        
        low_stock_products = self.env['product.product'].search(low_stock_domain, limit=10)
        
        # Get products with highest stock value
        all_products = self.env['product.product'].search([
            ('type', '=', 'product'),
            ('company_id', '=', self.company_id.id)
        ])
        
        stock_values = [(p, p.qty_available * p.standard_price) for p in all_products]
        top_value_products = sorted(stock_values, key=lambda x: x[1], reverse=True)[:5]
        
        # Get recent stock moves
        today = fields.Date.today()
        one_month_ago = today - timedelta(days=30)
        
        recent_moves = self.env['stock.move'].search([
            ('company_id', '=', self.company_id.id),
            ('date', '>=', one_month_ago),
            ('state', '=', 'done')
        ], order='date desc', limit=5)
        
        # Format data
        result = f"""
Inventory Data (as of {today}):

Products with Low Stock (less than 10 units):
"""
        if low_stock_products:
            for i, product in enumerate(low_stock_products, 1):
                result += f"{i}. {product.name}: {product.qty_available} {product.uom_id.name}\n"
        else:
            result += "No products with low stock found.\n"
        
        result += "\nProducts with Highest Stock Value:"
        if top_value_products:
            for i, (product, value) in enumerate(top_value_products, 1):
                result += f"\n{i}. {product.name}: {product.qty_available} {product.uom_id.name}, Value: {value:.2f}"
        else:
            result += "\nNo products found."
        
        result += "\n\nRecent Stock Movements:"
        if recent_moves:
            for move in recent_moves:
                result += f"\n- {move.date.strftime('%Y-%m-%d')}: {move.product_id.name}, {move.product_uom_qty} {move.product_uom.name}, {move.location_id.name} → {move.location_dest_id.name}"
        else:
            result += "\nNo recent stock movements found."
        
        return result
    
    def _get_finance_data(self, message):
        """Get finance data based on the user's message"""
        # Determine time period from message
        time_period = self._extract_time_period(message)
        
        # Get date range for the period
        date_from, date_to = self._get_date_range(time_period)
        
        # Query invoices for the period
        invoice_domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        invoices = self.env['account.move'].search(invoice_domain)
        
        # Query bills for the period
        bill_domain = [
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        bills = self.env['account.move'].search(bill_domain)
        
        # Query payments for the period
        payment_domain = [
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        payments = self.env['account.payment'].search(payment_domain)
        
        # Calculate metrics
        total_invoices = len(invoices)
        total_invoice_amount = sum(invoices.mapped('amount_total'))
        
        total_bills = len(bills)
        total_bill_amount = sum(bills.mapped('amount_total'))
        
        total_customer_payments = sum(p.amount for p in payments if p.partner_type == 'customer')
        total_vendor_payments = sum(p.amount for p in payments if p.partner_type == 'supplier')
        
        # Get overdue invoices
        today = fields.Date.today()
        overdue_domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date_due', '<', today),
            ('payment_state', 'in', ['not_paid', 'partial']),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        overdue_invoices = self.env['account.move'].search(overdue_domain, order='invoice_date_due')
        
        # Format data
        result = f"""
Finance Data ({time_period}):
- Date Range: {date_from.strftime('%Y-%m-%d')} to {date_to.strftime('%Y-%m-%d')}

Invoices:
- Total Invoices: {total_invoices}
- Total Value: {total_invoice_amount:.2f}
- Average Value: {(total_invoice_amount / total_invoices if total_invoices else 0):.2f}

Bills:
- Total Bills: {total_bills}
- Total Value: {total_bill_amount:.2f}
- Average Value: {(total_bill_amount / total_bills if total_bills else 0):.2f}

Payments:
- Customer Payments: {total_customer_payments:.2f}
- Vendor Payments: {total_vendor_payments:.2f}

Overdue Invoices:
"""
        if overdue_invoices:
            for i, inv in enumerate(overdue_invoices[:5], 1):
                days_overdue = (today - inv.invoice_date_due).days
                result += f"{i}. {inv.name}: {inv.partner_id.name}, {inv.amount_total:.2f}, {days_overdue} days overdue\n"
                
            if len(overdue_invoices) > 5:
                result += f"... and {len(overdue_invoices) - 5} more\n"
        else:
            result += "No overdue invoices found.\n"
        
        return result
    
    def _extract_time_period(self, message):
        """Extract time period from message with enhanced detection"""
        message_lower = message.lower()
        
        # Define months in both English and Indonesian
        months_en = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december']
        months_id = ['januari', 'februari', 'maret', 'april', 'mei', 'juni', 'juli', 'agustus', 'september', 'oktober', 'november', 'desember']
        
        # Map month names to numbers
        month_to_num = {}
        for i, month in enumerate(months_en, 1):
            month_to_num[month] = i
        for i, month in enumerate(months_id, 1):
            month_to_num[month] = i
        
        # Check for specific month pattern (e.g., "January 2025" or "Januari 2025")
        for month_name in list(month_to_num.keys()):
            if month_name in message_lower:
                # Look for a year following the month
                year_pattern = rf"{month_name}\s+(\d{{4}})"
                year_match = re.search(year_pattern, message_lower)
                
                if year_match:
                    year = int(year_match.group(1))
                    month = month_to_num[month_name]
                    
                    # Return specific month/year object
                    return {
                        'type': 'specific_month',
                        'month': month,
                        'year': year
                    }
        
        # Define time periods and their keywords
        time_periods = {
            'today': ['today', 'hari ini'],
            'yesterday': ['yesterday', 'kemarin'],
            'this_week': ['this week', 'minggu ini', 'week'],
            'last_week': ['last week', 'minggu lalu'],
            'this_month': ['this month', 'bulan ini', 'month'],
            'last_month': ['last month', 'bulan lalu'],
            'this_quarter': ['this quarter', 'kuartal ini', 'quarter'],
            'last_quarter': ['last quarter', 'kuartal lalu'],
            'this_year': ['this year', 'tahun ini', 'year'],
            'last_year': ['last year', 'tahun lalu'],
        }
        
        # Check for each period
        for period, keywords in time_periods.items():
            if any(keyword in message_lower for keyword in keywords):
                return period
        
        # Look for range pattern (e.g., "from January to March 2025")
        range_pattern = r"(?:from|dari)\s+(\w+)\s+(?:to|sampai|hingga)\s+(\w+)(?:\s+(\d{4}))?"
        range_match = re.search(range_pattern, message_lower)
        
        if range_match:
            start_month = range_match.group(1)
            end_month = range_match.group(2)
            year = int(range_match.group(3)) if range_match.group(3) else datetime.now().year
            
            if start_month in month_to_num and end_month in month_to_num:
                return {
                    'type': 'date_range',
                    'start_month': month_to_num[start_month],
                    'end_month': month_to_num[end_month],
                    'year': year
                }
        
        # Default to this month
        return 'this_month'
            # return {'type': 'relative', 'period': 'this_month'}
    
    def _get_date_range(self, time_period):
        """Get date range for the given time period with enhanced format support"""
        today = fields.Date.today()
        
        # Handle complex time period object
        if isinstance(time_period, dict):
            period_type = time_period.get('type')
            
            # Handle specific month
            if period_type == 'specific_month':
                year = time_period.get('year')
                month = time_period.get('month')
                
                start_date = datetime(year, month, 1).date()
                
                # Calculate end of month
                if month == 12:
                    end_month = datetime(year+1, 1, 1).date() - timedelta(days=1)
                else:
                    end_month = datetime(year, month+1, 1).date() - timedelta(days=1)
                    
                return start_date, end_month
                
            # Handle date range
            elif period_type == 'date_range':
                year = time_period.get('year')
                start_month = time_period.get('start_month')
                end_month = time_period.get('end_month')
                
                start_date = datetime(year, start_month, 1).date()
                
                # Calculate end of end_month
                if end_month == 12:
                    end_date = datetime(year+1, 1, 1).date() - timedelta(days=1)
                else:
                    end_date = datetime(year, end_month+1, 1).date() - timedelta(days=1)
                    
                return start_date, end_date
        
        # Handle string time periods (existing code)
        if time_period == 'today':
            return today, today
        elif time_period == 'yesterday':
            yesterday = today - timedelta(days=1)
            return yesterday, yesterday
        elif time_period == 'this_week':
            start_of_week = today - timedelta(days=today.weekday())
            return start_of_week, today
        elif time_period == 'last_week':
            end_of_last_week = today - timedelta(days=today.weekday() + 1)
            start_of_last_week = end_of_last_week - timedelta(days=6)
            return start_of_last_week, end_of_last_week
        elif time_period == 'this_month':
            start_of_month = today.replace(day=1)
            return start_of_month, today
        elif time_period == 'last_month':
            last_month_end = today.replace(day=1) - timedelta(days=1)
            last_month_start = last_month_end.replace(day=1)
            return last_month_start, last_month_end
        elif time_period == 'this_quarter':
            current_quarter = ((today.month - 1) // 3) + 1
            start_month = 3 * (current_quarter - 1) + 1
            start_of_quarter = today.replace(month=start_month, day=1)
            return start_of_quarter, today
        elif time_period == 'last_quarter':
            current_quarter = ((today.month - 1) // 3) + 1
            last_quarter = current_quarter - 1 if current_quarter > 1 else 4
            last_quarter_year = today.year if current_quarter > 1 else today.year - 1
            start_month = 3 * (last_quarter - 1) + 1
            end_month = start_month + 2
            start_date = datetime(last_quarter_year, start_month, 1).date()
            end_date = datetime(last_quarter_year, end_month, 1).date() + timedelta(days=31)
            end_date = end_date.replace(day=1) - timedelta(days=1)
            return start_date, end_date
        elif time_period == 'this_year':
            start_of_year = today.replace(month=1, day=1)
            return start_of_year, today
        elif time_period == 'last_year':
            last_year = today.year - 1
            start_date = datetime(last_year, 1, 1).date()
            end_date = datetime(last_year, 12, 31).date()
            return start_date, end_date
        
        # Default to this month
        start_of_month = today.replace(day=1)
        return start_of_month, today
    
class AIService(models.AbstractModel):
    _name = 'ai.service'
    _description = 'AI Service'
    
    def get_ai_response(self, chat, message, model=None, business_context=None):
        """
        Mendapatkan respons dari AI untuk pesan yang diberikan
        """
        try:
            # Gunakan metode send_message yang sudah ada di model ai.chat
            return chat.send_message(message, model)
            
        except Exception as e:
            _logger.error(f"Error getting AI response: {str(e)}")
            return {'error': str(e)}
