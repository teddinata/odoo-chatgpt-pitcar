from odoo import models, fields, api, _, exceptions, tools
from odoo.exceptions import ValidationError, AccessError, UserError
from openai import OpenAI
import logging
import json
import pytz
from datetime import datetime, timedelta, date
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
        """Get service lead time analysis with proper None handling"""
        try:
            # Jika date_from atau date_to adalah None, ekstrak dari pesan
            if date_from is None or date_to is None:
                # Analisis periode waktu dari pesan
                time_period = self._extract_time_period(message)
                date_from, date_to = self._get_date_range(time_period)
            
            # Tambahkan validasi tambahan untuk memastikan tanggal valid
            if not date_from or not date_to:
                return "Tidak dapat menganalisis lead time: periode waktu tidak valid."
                
            # Validasi bahwa date_from dan date_to memiliki tipe data yang benar
            if not isinstance(date_from, (datetime, date)) or not isinstance(date_to, (datetime, date)):
                # Log data tipe untuk debug
                _logger.error(f"Invalid date types - date_from: {type(date_from)}, date_to: {type(date_to)}")
                # Handle tipe data yang salah dengan menggunakan tanggal default
                now = fields.Date.today()
                start_of_month = now.replace(day=1)
                date_from, date_to = start_of_month, now
                
            # Cari semua order dengan service selesai dalam rentang waktu
            orders = self.env['sale.order'].search([
                ('controller_selesai', '>=', date_from),
                ('controller_selesai', '<=', date_to),
            ])
            
            if not orders:
                # Format tanggal dengan aman
                from_str = date_from.strftime('%Y-%m-%d') if hasattr(date_from, 'strftime') else str(date_from)
                to_str = date_to.strftime('%Y-%m-%d') if hasattr(date_to, 'strftime') else str(date_to)
                return f"\n\nAnalisis Lead Time Servis ({from_str} hingga {to_str}):\nTidak ditemukan data service untuk periode waktu ini."
            
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
        """Get comprehensive business data covering multiple aspects including predictive analysis"""
        try:
            # Analisis periode waktu dari pesan
            time_period = self._extract_time_period(message)
            date_from, date_to = self._get_date_range(time_period)

            # Validasi tanggal
            today = fields.Date.today()
            if date_from > today or date_to > today:
                # Log warning untuk tanggal future
                _logger.warning(f"Future date requested in comprehensive data: {date_from} to {date_to}")
                # Optional: Ganti dengan tanggal yang valid dan beri tahu user
                message_note = "\n(Catatan: Analisis untuk tanggal future diganti dengan data periode terakhir yang tersedia)\n"
                
                # Atur ulang tanggal ke periode bulan lalu
                last_month_end = today.replace(day=1) - timedelta(days=1)
                last_month_start = last_month_end.replace(day=1)
                date_from, date_to = last_month_start, last_month_end
            
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
            
            # 5. ANALISIS PRODUK
            product_analysis = self._get_product_analysis(date_from, date_to)
            result += product_analysis + "\n\n"
            
            # 6. ANALISIS PELANGGAN (NEW)
            try:
                behavior_data = self._get_customer_behavior_analysis(message)
                if behavior_data:
                    result += "ANALISIS PERILAKU PELANGGAN:\n"
                    result += behavior_data + "\n\n"
                
                # RFM Analysis (NEW)
                rfm_data = self._get_rfm_analysis(message)
                if rfm_data:
                    result += "ANALISIS RFM PELANGGAN:\n"
                    result += rfm_data + "\n\n"
            except Exception as e:
                _logger.error(f"Error getting customer analysis data: {str(e)}")
            
            # 7. ANALISIS BOOKING
            booking_data = self._get_booking_data(message)
            if booking_data:
                result += "ANALISIS BOOKING SERVIS:\n"
                result += booking_data + "\n\n"
            
            # 8. ANALISIS REKOMENDASI SERVIS
            recommendation_data = self._get_service_recommendation_data(message)
            if recommendation_data:
                result += "ANALISIS REKOMENDASI SERVIS:\n"
                result += recommendation_data + "\n\n"
            
            # 9. PREDIKSI & PROYEKSI (NEW)
            try:
                prediction_data = self._get_sales_prediction(message)
                if prediction_data:
                    result += "PREDIKSI & PROYEKSI:\n"
                    result += prediction_data + "\n\n"
            except Exception as e:
                _logger.error(f"Error getting prediction data: {str(e)}")
            
            # 10. ANALISIS PELUANG BISNIS (NEW)
            try:
                opportunity_data = self._get_business_opportunity_analysis(message)
                if opportunity_data:
                    result += "ANALISIS PELUANG BISNIS:\n"
                    result += opportunity_data + "\n\n"
            except Exception as e:
                _logger.error(f"Error getting business opportunity data: {str(e)}")
            
            # 11. ANALISIS WORKFLOW & EFISIENSI (NEW)
            try:
                workflow_data = self._get_workflow_efficiency_analysis(message)
                if workflow_data:
                    result += "ANALISIS WORKFLOW & EFISIENSI:\n"
                    result += workflow_data + "\n\n"
            except Exception as e:
                _logger.error(f"Error getting workflow efficiency data: {str(e)}")
            
            # 12. METRIK KUALITAS
            sa_performance = self._get_service_advisor_performance(date_from, date_to, message)
            mechanic_performance = self._get_mechanic_performance(date_from, date_to, message)
            
            result += "METRIK KUALITAS:\n"
            result += "- Service Advisor Performance:\n"
            result += sa_performance + "\n"
            result += "- Mechanic Performance:\n"
            result += mechanic_performance + "\n\n"
            
            # 13. REKOMENDASI & INSIGHT STRATEGIS
            result += "REKOMENDASI & INSIGHT STRATEGIS:\n"

            # 14. ANALISIS CUSTOMER
            # Tambahkan bagian analisis customer
            retention_metrics = {}
            try:
                customer_overview = self._get_customer_overview(date_from, date_to)
                if customer_overview:
                    result += "ANALISIS PELANGGAN:\n"
                    result += customer_overview + "\n\n"
                
                # Tambahkan segmentasi pelanggan
                customer_segmentation = self._get_customer_segmentation(date_from, date_to, message)
                if customer_segmentation:
                    result += "SEGMENTASI PELANGGAN:\n"
                    result += customer_segmentation + "\n\n"
                
                # Tambahkan analisis retensi dan simpan metrik retensi untuk digunakan nanti
                customer_retention = self._get_customer_retention_analysis(date_from, date_to, message)
                if customer_retention:
                    result += "RETENSI & LOYALITAS PELANGGAN:\n"
                    result += customer_retention + "\n\n"
                    
                    # Extract retention rate dari hasil analisis
                    # Cari baris yang berisi "Tingkat Retensi: X%" dan ekstrak nilainya
                    retention_lines = [line for line in customer_retention.split('\n') if "Tingkat Retensi:" in line]
                    if retention_lines:
                        try:
                            # Ekstrak angka persentase
                            retention_text = retention_lines[0].split(':')[1].strip()
                            retention_metrics['retention_rate'] = float(retention_text.replace('%', ''))
                        except (IndexError, ValueError):
                            retention_metrics['retention_rate'] = 0
            except Exception as e:
                _logger.error(f"Error getting customer analysis data: {str(e)}")
            
            # Combine insights from multiple analyses
            insights = []
            
            # From attendance data
            if metrics.get('attendance_rate', 0) < 90:
                insights.append("- Tingkat kehadiran di bawah 90%, perlu ditingkatkan kedisiplinan karyawan")
            
            # From customer satisfaction
            if metrics.get('avg_customer_rating', 0) < 4:
                insights.append("- Rating pelanggan di bawah 4/5, perlu peningkatan kualitas layanan")
            
            # From RFM Analysis (NEW)
            try:
                # Get RFM insights if available
                if 'rfm_data' in locals() and rfm_data and 'Champions' in rfm_data:
                    insights.append("- Fokus pada program retensi untuk mempertahankan segmen 'Champions' dan meningkatkan loyalitas")
                elif 'rfm_data' in locals() and rfm_data and 'At Risk' in rfm_data:
                    insights.append("- Implementasikan program win-back untuk segmen 'At Risk' yang memiliki potensi churn tinggi")
            except:
                pass
            
            # From product & sales analysis
            try:
                # Extract top growth product if available
                growth_query = """
                    WITH monthly_sales AS (
                        SELECT
                            p.id as product_id,
                            p.name as product_name,
                            DATE_TRUNC('month', so.date_order)::date as month,
                            SUM(sol.product_uom_qty) as quantity,
                            SUM(sol.price_subtotal) as revenue
                        FROM
                            sale_order_line sol
                        JOIN
                            sale_order so ON sol.order_id = so.id
                        JOIN
                            product_product p ON sol.product_id = p.id
                        WHERE
                            so.date_order >= %s AND
                            so.date_order <= %s AND
                            so.state in ('sale', 'done')
                        GROUP BY
                            p.id, p.name, DATE_TRUNC('month', so.date_order)
                        ORDER BY
                            month
                    )
                    SELECT
                        product_name,
                        SUM(revenue) as total_revenue
                    FROM
                        monthly_sales
                    GROUP BY
                        product_name
                    ORDER BY
                        total_revenue DESC
                    LIMIT 1
                """
                
                self.env.cr.execute(growth_query, (date_from, date_to))
                top_product = self.env.cr.dictfetchone()
                
                if top_product:
                    insights.append(f"- Fokus pengembangan pada produk/layanan '{top_product['product_name']}' yang menunjukkan performa tertinggi")
            except:
                pass
            
            # From workflow analysis
            try:
                job_stop_query = """
                    SELECT
                        AVG(lead_time_tunggu_konfirmasi) as avg_confirmation_wait,
                        AVG(lead_time_tunggu_part1) as avg_part1_wait,
                        AVG(lead_time_tunggu_part2) as avg_part2_wait,
                        AVG(lead_time_tunggu_sublet) as avg_sublet_wait
                    FROM
                        sale_order
                    WHERE
                        controller_selesai >= %s AND
                        controller_selesai <= %s AND
                        controller_mulai_servis IS NOT NULL AND
                        controller_selesai IS NOT NULL
                """
                
                self.env.cr.execute(job_stop_query, (date_from, date_to))
                job_stop_data = self.env.cr.dictfetchall()
                
                if job_stop_data:
                    data = job_stop_data[0]
                    job_stops = [
                        ('Tunggu Konfirmasi', data['avg_confirmation_wait'] or 0),
                        ('Tunggu Part 1', data['avg_part1_wait'] or 0),
                        ('Tunggu Part 2', data['avg_part2_wait'] or 0),
                        ('Tunggu Sublet', data['avg_sublet_wait'] or 0)
                    ]
                    
                    job_stops_sorted = sorted(job_stops, key=lambda x: x[1], reverse=True)
                    
                    if job_stops_sorted[0][1] > 1.0:  # If highest job stop > 1 hour
                        bottleneck = job_stops_sorted[0][0]
                        insights.append(f"- Optimalkan proses '{bottleneck}' yang merupakan bottleneck utama dalam workflow servis")
            except:
                pass
            
            # From business opportunity analysis (NEW)
            if 'opportunity_data' in locals() and opportunity_data:
                insights.append("- Evaluasi peluang pengembangan bisnis berdasarkan analisis pertumbuhan pasar dan segmen pelanggan")
            
            # From prediction data (NEW)
            if 'prediction_data' in locals() and prediction_data:
                insights.append("- Sesuaikan kapasitas dan inventory berdasarkan proyeksi penjualan untuk periode mendatang")

            # Tambahkan insights berbasis customer dengan pengecekan yang aman
            if retention_metrics.get('retention_rate') is not None:
                if retention_metrics['retention_rate'] < 50:
                    insights.append("- Fokus pada peningkatan retensi pelanggan yang saat ini di bawah 50%")
                else:
                    insights.append("- Kembangkan program loyalitas untuk mempertahankan tingkat retensi yang baik")
            
            if 'customer_overview' in locals() and customer_overview:
                insights.append("- Lakukan segmentasi pelanggan untuk personalisasi penawaran dan komunikasi")
            
            # Add generic strategic insights
            insights.extend([
                "- Implementasikan program loyalitas untuk pelanggan dengan frekuensi servis tinggi",
                "- Optimalkan jadwal mekanik berdasarkan performa lead time",
                "- Evaluasi strategi harga untuk layanan dengan margin tertinggi",
                "- Tingkatkan kapasitas pada jam sibuk berdasarkan pola booking"
            ])
            
            # Add all insights
            for insight in insights:
                result += insight + "\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error getting comprehensive data: {str(e)}")
            return f"\nError mendapatkan data komprehensif: {str(e)}"
        
    def _get_product_data(self, message):
        """Get product data based on the user's message"""
        try:
            # Analisis kategori produk yang mungkin disebutkan dalam pesan
            product_keywords = ['product', 'item', 'produk', 'barang', 'part', 'sparepart']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in product_keywords):
                return None
                
            # Cari kategori yang mungkin disebutkan
            categories = self.env['product.category'].search([])
            mentioned_categories = []
            
            for category in categories:
                if category.name.lower() in message_lower:
                    mentioned_categories.append(category.id)
            
            # Bangun domain pencarian
            domain = [('type', 'in', ['product', 'service'])]
            
            if mentioned_categories:
                domain.append(('categ_id', 'in', mentioned_categories))
            
            # Tentukan jumlah produk untuk ditampilkan
            limit = 15
            if 'all' in message_lower or 'semua' in message_lower:
                limit = 50
            
            # Cari produk berdasarkan domain
            products = self.env['product.product'].search(domain, limit=limit)
            
            if not products:
                return "Tidak ditemukan data produk yang sesuai dengan permintaan."
            
            # Format output
            result = f"\n\nData Produk ({len(products)} produk):\n\n"
            
            # Kelompokkan produk berdasarkan kategori untuk tampilan yang lebih terorganisir
            products_by_category = {}
            for product in products:
                category_name = product.categ_id.name
                if category_name not in products_by_category:
                    products_by_category[category_name] = []
                products_by_category[category_name].append(product)
            
            # Tampilkan produk berdasarkan kategori
            for category, category_products in products_by_category.items():
                result += f"Kategori: {category}\n"
                for product in category_products:
                    result += f"- {product.name}\n"
                    result += f"  Harga: {product.list_price:,.2f}\n"
                    result += f"  Stok: {product.qty_available}\n"
                    
                    # Tampilkan durasi service jika produk adalah layanan
                    if product.type == 'service' and hasattr(product, 'service_duration') and product.service_duration:
                        result += f"  Durasi Layanan: {product.service_duration} jam\n"
                    
                    # Tampilkan informasi umur inventori jika produk adalah barang fisik
                    if product.type == 'product' and hasattr(product, 'inventory_age_days') and product.inventory_age_days:
                        result += f"  Umur Persediaan: {product.inventory_age_days} hari\n"
                        
                    # Tampilkan informasi "wajib ready stock" jika properti tersedia
                    if hasattr(product, 'is_mandatory_stock') and product.is_mandatory_stock:
                        result += f"  Wajib Ready: Ya (Min: {product.min_mandatory_stock})\n"
                    
                    result += "\n"
            
            # Tambahkan statistik ringkasan
            service_products = products.filtered(lambda p: p.type == 'service')
            physical_products = products.filtered(lambda p: p.type == 'product')
            
            result += f"\nRingkasan:\n"
            result += f"- Total Produk Layanan: {len(service_products)}\n"
            result += f"- Total Produk Fisik: {len(physical_products)}\n"
            
            # Hitung nilai total persediaan
            if physical_products:
                total_stock_value = sum(p.qty_available * p.standard_price for p in physical_products)
                result += f"- Nilai Total Persediaan: {total_stock_value:,.2f}\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan data produk: {str(e)}"
        
    def _get_booking_data(self, message):
        """Get service booking data based on the user's message"""
        try:
            booking_keywords = ['booking', 'reservasi', 'janji', 'appointment', 'jadwal']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in booking_keywords):
                return None
                
            # Analisis periode waktu dari pesan
            time_period = self._extract_time_period(message)
            date_from, date_to = self._get_date_range(time_period)
            
            # Cari booking dalam rentang waktu
            bookings = self.env['pitcar.service.booking'].search([
                ('booking_date', '>=', date_from),
                ('booking_date', '<=', date_to)
            ], order='booking_date, booking_time')
            
            if not bookings:
                return f"\n\nData Booking Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan booking servis untuk periode waktu ini."
            
            # Agregasi data booking
            total_bookings = len(bookings)
            states = {}
            for booking in bookings:
                state = dict(booking._fields['state'].selection).get(booking.state, booking.state)
                states[state] = states.get(state, 0) + 1
            
            # Hitung booking berdasarkan stall
            stalls = {}
            for booking in bookings:
                if hasattr(booking, 'stall_position') and booking.stall_position:
                    stall = dict(booking._fields['stall_position'].selection).get(booking.stall_position, booking.stall_position)
                    stalls[stall] = stalls.get(stall, 0) + 1
            
            # Group by kategori servis
            categories = {}
            for booking in bookings:
                if hasattr(booking, 'service_category') and booking.service_category:
                    category = dict(booking._fields['service_category'].selection).get(booking.service_category, booking.service_category)
                    categories[category] = categories.get(category, 0) + 1
            
            # Format output
            result = f"\n\nData Booking Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            result += f"- Total Booking: {total_bookings}\n\n"
            
            # Tampilkan status booking
            result += "Status Booking:\n"
            for state, count in states.items():
                result += f"- {state}: {count} ({count/total_bookings*100:.1f}%)\n"
            
            # Tampilkan kategori servis
            if categories:
                result += "\nKategori Servis:\n"
                for category, count in categories.items():
                    result += f"- {category}: {count} ({count/total_bookings*100:.1f}%)\n"
            
            # Tampilkan distribusi stall
            if stalls:
                result += "\nDistribusi Stall:\n"
                for stall, count in stalls.items():
                    result += f"- {stall}: {count} ({count/total_bookings*100:.1f}%)\n"
            
            # Tampilkan booking untuk 3 hari ke depan jika ada
            today = fields.Date.today()
            upcoming_bookings = self.env['pitcar.service.booking'].search([
                ('booking_date', '>=', today),
                ('booking_date', '<=', today + timedelta(days=3)),
                ('state', '=', 'confirmed')
            ], order='booking_date, booking_time')
            
            if upcoming_bookings:
                result += f"\nBooking 3 Hari ke Depan ({len(upcoming_bookings)}):\n"
                current_date = None
                for booking in upcoming_bookings:
                    if current_date != booking.booking_date:
                        current_date = booking.booking_date
                        result += f"\n  {current_date.strftime('%A, %d %B %Y')}:\n"
                    
                    result += f"  - {booking.formatted_time}: {booking.partner_id.name}"
                    if hasattr(booking, 'partner_car_id') and booking.partner_car_id:
                        result += f" ({booking.partner_car_id.name})"
                    result += "\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan data booking servis: {str(e)}"
        
    def _get_service_recommendation_data(self, message):
        """Get service recommendation data based on the user's message"""
        try:
            recommendation_keywords = ['recommendation', 'rekomendasi', 'saran', 'suggest']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in recommendation_keywords):
                return None
                
            # Analisis periode waktu dari pesan
            time_period = self._extract_time_period(message)
            date_from, date_to = self._get_date_range(time_period)
            
            # Cari rekomendasi dalam rentang waktu (menggunakan tanggal order)
            orders = self.env['sale.order'].search([
                ('create_date', '>=', date_from),
                ('create_date', '<=', date_to),
                ('recommendation_ids', '!=', False)
            ])
            
            if not orders:
                return f"\n\nData Rekomendasi Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan rekomendasi servis untuk periode waktu ini."
            
            # Kumpulkan semua rekomendasi
            recommendations = self.env['sale.order.recommendation'].search([
                ('order_id', 'in', orders.ids)
            ])
            
            if not recommendations:
                return f"\n\nData Rekomendasi Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\nTidak ditemukan detail rekomendasi untuk periode waktu ini."
            
            # Agregasi data rekomendasi
            total_recommendations = len(recommendations)
            total_value = sum(recommendations.mapped('total_amount'))
            
            # Analisis status rekomendasi
            states = {}
            for rec in recommendations:
                state = dict(rec._fields['state'].selection).get(rec.state, rec.state)
                states[state] = states.get(state, 0) + 1
            
            # Analisis produk yang paling sering direkomendasikan
            recommended_products = {}
            for rec in recommendations:
                product_name = rec.product_id.name
                if product_name in recommended_products:
                    recommended_products[product_name]['count'] += 1
                    recommended_products[product_name]['value'] += rec.total_amount
                else:
                    recommended_products[product_name] = {
                        'count': 1,
                        'value': rec.total_amount
                    }
            
            # Sortir produk berdasarkan frekuensi rekomendasi
            top_products = sorted(
                recommended_products.items(),
                key=lambda x: x[1]['count'],
                reverse=True
            )[:10]
            
            # Format output
            result = f"\n\nData Rekomendasi Servis ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n"
            result += f"- Total Rekomendasi: {total_recommendations}\n"
            result += f"- Nilai Total: {total_value:,.2f}\n"
            result += f"- Rata-rata Per Rekomendasi: {total_value/total_recommendations:,.2f}\n\n"
            
            # Tampilkan status rekomendasi
            result += "Status Rekomendasi:\n"
            for state, count in states.items():
                result += f"- {state}: {count} ({count/total_recommendations*100:.1f}%)\n"
            
            # Tampilkan top produk yang direkomendasikan
            result += "\nProduk Paling Sering Direkomendasikan:\n"
            for i, (product, data) in enumerate(top_products, 1):
                result += f"{i}. {product}: {data['count']} kali ({data['value']:,.2f})\n"
            
            # Analisis conversion rate
            scheduled_count = states.get('scheduled', 0)
            conversion_rate = (scheduled_count / total_recommendations) * 100 if total_recommendations else 0
            
            result += f"\nTingkat Konversi Rekomendasi: {conversion_rate:.2f}%\n"
            
            return result
            
        except Exception as e:
            return f"\n\nError mendapatkan data rekomendasi servis: {str(e)}"
        
    def _get_product_analysis(self, date_from, date_to):
        """Get product analysis for comprehensive report"""
        try:
            # Get top selling products
            sale_lines = self.env['sale.order.line'].search([
                ('order_id.date_order', '>=', date_from),
                ('order_id.date_order', '<=', date_to),
                ('order_id.state', 'in', ['sale', 'done']),
                ('product_id', '!=', False)
            ])
            
            # Group by product
            product_sales = {}
            for line in sale_lines:
                product_id = line.product_id.id
                if product_id not in product_sales:
                    product_sales[product_id] = {
                        'name': line.product_id.name,
                        'quantity': 0,
                        'amount': 0,
                        'category': line.product_id.categ_id.name
                    }
                product_sales[product_id]['quantity'] += line.product_uom_qty
                product_sales[product_id]['amount'] += line.price_subtotal
            
            # Sort by amount
            top_products = sorted(
                product_sales.values(),
                key=lambda x: x['amount'],
                reverse=True
            )[:10]
            
            # Group by category
            category_sales = {}
            for product in product_sales.values():
                category = product['category']
                if category not in category_sales:
                    category_sales[category] = {
                        'quantity': 0,
                        'amount': 0
                    }
                category_sales[category]['quantity'] += product['quantity']
                category_sales[category]['amount'] += product['amount']
            
            # Sort by amount
            top_categories = sorted(
                [(k, v) for k, v in category_sales.items()],
                key=lambda x: x[1]['amount'],
                reverse=True
            )[:5]
            
            # Get inventory metrics if available
            has_inventory_age = False
            aged_inventory = None
            
            if hasattr(self.env['product.product'], 'inventory_age_category'):
                has_inventory_age = True
                aged_inventory = self.env['product.product'].read_group(
                    [('type', '=', 'product'), ('qty_available', '>', 0)],
                    ['qty_available:sum', 'standard_price:avg'],
                    ['inventory_age_category']
                )
            
            # Format the result
            result = "ANALISIS PRODUK:\n"
            
            # Top selling products
            result += "Top 10 Produk Terlaris:\n"
            for i, product in enumerate(top_products, 1):
                result += f"{i}. {product['name']}: {product['quantity']} unit, Rp {product['amount']:,.2f}\n"
            
            # Top categories
            result += "\nTop 5 Kategori Produk:\n"
            for i, (category, data) in enumerate(top_categories, 1):
                result += f"{i}. {category}: {data['quantity']} unit, Rp {data['amount']:,.2f}\n"
            
            # Inventory age analysis
            if has_inventory_age and aged_inventory:
                result += "\nAnalisis Umur Persediaan:\n"
                for age_group in aged_inventory:
                    category = age_group['inventory_age_category'] or 'Tidak Terklasifikasi'
                    qty = age_group['qty_available'] or 0
                    avg_cost = age_group['standard_price'] or 0
                    value = qty * avg_cost
                    
                    category_name = dict(self.env['product.template']._fields['inventory_age_category'].selection).get(category, category)
                    result += f"- {category_name}: {qty} unit, Nilai: Rp {value:,.2f}\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error in product analysis: {str(e)}")
            return "Error mendapatkan analisis produk."
        
    def _get_sales_prediction(self, message):
        """Generate sales prediction based on historical data"""
        try:
            import numpy as np
            from sklearn.linear_model import LinearRegression
            from datetime import datetime, date, timedelta
            import pandas as pd
            
            # Validasi bahwa ini adalah query prediksi
            prediction_keywords = ['predict', 'forecast', 'projection', 'prediksi', 'proyeksi', 'perkiraan', 'future', 'masa depan']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in prediction_keywords):
                return None
                
            # Tentukan periode analisis historis dan proyeksi
            history_months = 6  # Analisis 6 bulan ke belakang
            forecast_months = 3  # Proyeksi 3 bulan ke depan
            
            # Jika disebutkan dalam pesan, gunakan nilai yang disebutkan
            for i in range(1, 13):
                if f"{i} bulan" in message_lower or f"{i} month" in message_lower:
                    if "projection" in message_lower or "forecast" in message_lower or "prediksi" in message_lower or "proyeksi" in message_lower:
                        forecast_months = i
                    else:
                        history_months = i
            
            # Tentukan rentang waktu untuk data historis
            today = fields.Date.today()
            start_date = today - timedelta(days=30*history_months)
            
            # Ambil data penjualan historis (berdasarkan bulan)
            query = """
                SELECT 
                    DATE_TRUNC('month', date_order)::date as month,
                    COUNT(id) as order_count,
                    SUM(amount_total) as total_sales
                FROM 
                    sale_order
                WHERE 
                    date_order >= %s AND
                    date_order <= %s AND
                    state in ('sale', 'done')
                GROUP BY 
                    DATE_TRUNC('month', date_order)
                ORDER BY 
                    month
            """
            
            self.env.cr.execute(query, (start_date, today))
            result = self.env.cr.dictfetchall()
            
            if not result or len(result) < 3:  # Minimal butuh 3 bulan data untuk prediksi
                return "Tidak cukup data historis untuk membuat prediksi yang akurat. Diperlukan minimal 3 bulan data penjualan."
            
            # Konversi ke format yang sesuai untuk analisis
            dates = []
            sales_values = []
            order_counts = []
            
            for r in result:
                month_date = r['month']
                dates.append(month_date)
                sales_values.append(r['total_sales'])
                order_counts.append(r['order_count'])
            
            # Buat array feature (x) - gunakan indeks bulan (0, 1, 2, ...)
            X = np.array(range(len(dates))).reshape(-1, 1)
            
            # Latih model untuk sales
            sales_model = LinearRegression()
            sales_model.fit(X, sales_values)
            
            # Latih model untuk jumlah order
            orders_model = LinearRegression()
            orders_model.fit(X, order_counts)
            
            # Buat proyeksi untuk bulan-bulan mendatang
            forecast_dates = []
            forecast_sales = []
            forecast_orders = []
            
            for i in range(1, forecast_months + 1):
                next_month = today.replace(day=1) + timedelta(days=32*i)
                next_month = next_month.replace(day=1)  # First day of the month
                
                month_index = len(dates) + i - 1
                pred_sales = sales_model.predict([[month_index]])[0]
                pred_orders = orders_model.predict([[month_index]])[0]
                
                forecast_dates.append(next_month)
                forecast_sales.append(pred_sales)
                forecast_orders.append(max(0, int(round(pred_orders))))
            
            # Hitung trend dan metrik pertumbuhan
            sales_trend = sales_model.coef_[0]
            orders_trend = orders_model.coef_[0]
            
            sales_growth_rate = (sales_trend / (sum(sales_values) / len(sales_values))) * 100
            orders_growth_rate = (orders_trend / (sum(order_counts) / len(order_counts))) * 100
            
            # Format output proyeksi
            result = f"\n\nPrediksi Penjualan ({forecast_months} Bulan ke Depan):\n\n"
            
            # Trend info
            result += "Tren Penjualan:\n"
            trend_desc = "Naik" if sales_growth_rate > 0 else "Turun"
            result += f"- Nilai Penjualan: {trend_desc} {abs(sales_growth_rate):.2f}% per bulan\n"
            
            trend_desc = "Naik" if orders_growth_rate > 0 else "Turun"
            result += f"- Jumlah Order: {trend_desc} {abs(orders_growth_rate):.2f}% per bulan\n\n"
            
            # Forecast data for each month
            result += "Proyeksi Bulanan:\n"
            for i in range(len(forecast_dates)):
                month_name = forecast_dates[i].strftime("%B %Y")
                result += f"- {month_name}:\n"
                result += f"  * Proyeksi Pendapatan: {forecast_sales[i]:,.2f}\n"
                result += f"  * Proyeksi Jumlah Order: {forecast_orders[i]}\n"
                result += f"  * Proyeksi Nilai Per Order: {forecast_sales[i]/forecast_orders[i]:,.2f}\n"
            
            # Provide insights based on predictions
            result += "\nRekomendasi Berdasarkan Proyeksi:\n"
            
            if sales_growth_rate > 5:
                result += "- Tren pertumbuhan positif yang kuat. Pertimbangkan untuk meningkatkan kapasitas layanan.\n"
            elif sales_growth_rate > 0:
                result += "- Tren pertumbuhan moderat. Pertahankan strategi penjualan saat ini.\n"
            else:
                result += "- Tren penurunan penjualan. Pertimbangkan promosi dan program loyalitas untuk meningkatkan penjualan.\n"
            
            # Calculate seasonal pattern if enough data
            if len(sales_values) >= 12:
                try:
                    # Simple seasonal decomposition
                    max_month_index = np.argmax(sales_values) % 12 + 1
                    min_month_index = np.argmin(sales_values) % 12 + 1
                    
                    max_month_name = date(2000, max_month_index, 1).strftime('%B')
                    min_month_name = date(2000, min_month_index, 1).strftime('%B')
                    
                    result += f"- Pola musiman terdeteksi: Penjualan tertinggi di bulan {max_month_name} dan terendah di bulan {min_month_name}.\n"
                except:
                    pass
            
            return result
            
        except ImportError:
            return "Modul untuk analisis prediktif tidak tersedia. Pastikan NumPy, pandas, dan scikit-learn telah diinstal."
        except Exception as e:
            _logger.error(f"Error in sales prediction: {str(e)}")
            return f"Error saat membuat prediksi penjualan: {str(e)}"
        
    def _get_customer_behavior_analysis(self, message):
        """Analyze customer behavior patterns and segmentation"""
        try:
            # Validasi bahwa ini adalah query analisis pelanggan
            customer_keywords = ['customer', 'pelanggan', 'behavior', 'perilaku', 'segmentation', 'segmentasi', 'loyal', 'loyalty', 'retention', 'retensi']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in customer_keywords):
                return None
                
            # Tentukan rentang waktu analisis
            today = fields.Date.today()
            analysis_period = 12  # Default: 12 bulan
            
            # Jika disebutkan dalam pesan, gunakan nilai yang disebutkan
            for i in range(1, 25):
                if f"{i} bulan" in message_lower or f"{i} month" in message_lower:
                    analysis_period = i
                    break
            
            start_date = today - timedelta(days=30*analysis_period)
            
            # Query untuk mendapatkan data order pelanggan
            customer_data = self.env['sale.order'].read_group(
                [('date_order', '>=', start_date), ('state', 'in', ['sale', 'done'])],
                ['partner_id', 'amount_total:sum', 'id:count'],
                ['partner_id']
            )
            
            if not customer_data:
                return f"Tidak ditemukan data transaksi pelanggan dalam {analysis_period} bulan terakhir."
            
            # Filter out entries without partner_id
            customer_data = [d for d in customer_data if d['partner_id']]
            
            total_customers = len(customer_data)
            total_revenue = sum(d['amount_total'] for d in customer_data)
            total_orders = sum(d['id'] for d in customer_data)
            
            # Calculate key metrics
            avg_order_value = total_revenue / total_orders if total_orders else 0
            avg_orders_per_customer = total_orders / total_customers if total_customers else 0
            avg_revenue_per_customer = total_revenue / total_customers if total_customers else 0
            
            # Segment customers by frequency
            order_frequency = {}
            for i in range(1, 11):  # 1-10 orders
                order_frequency[i] = len([d for d in customer_data if d['id'] == i])
            order_frequency['11+'] = len([d for d in customer_data if d['id'] > 10])
            
            # Segment customers by revenue
            revenue_segments = [
                {'name': 'Low Value (< 1 juta)', 'count': 0, 'min': 0, 'max': 1000000},
                {'name': 'Medium Value (1-5 juta)', 'count': 0, 'min': 1000000, 'max': 5000000},
                {'name': 'High Value (5-10 juta)', 'count': 0, 'min': 5000000, 'max': 10000000},
                {'name': 'Premium (> 10 juta)', 'count': 0, 'min': 10000000, 'max': float('inf')}
            ]
            
            for d in customer_data:
                for segment in revenue_segments:
                    if segment['min'] <= d['amount_total'] < segment['max']:
                        segment['count'] += 1
                        break
            
            # Identify customers with decreasing engagement (active but decreasing)
            recent_orders = {}
            older_orders = {}
            
            # Split period into recent half and older half
            mid_date = today - timedelta(days=30*analysis_period/2)
            
            # Query for recent orders (second half of period)
            recent_data = self.env['sale.order'].read_group(
                [('date_order', '>=', mid_date), ('date_order', '<=', today), ('state', 'in', ['sale', 'done'])],
                ['partner_id', 'id:count'],
                ['partner_id']
            )
            
            for d in recent_data:
                if d['partner_id']:
                    recent_orders[d['partner_id'][0]] = d['id']
            
            # Query for older orders (first half of period)
            older_data = self.env['sale.order'].read_group(
                [('date_order', '>=', start_date), ('date_order', '<', mid_date), ('state', 'in', ['sale', 'done'])],
                ['partner_id', 'id:count'],
                ['partner_id']
            )
            
            for d in older_data:
                if d['partner_id']:
                    older_orders[d['partner_id'][0]] = d['id']
            
            # Calculate engagement shifts
            increasing_engagement = 0
            decreasing_engagement = 0
            steady_engagement = 0
            
            for partner_id in set(list(recent_orders.keys()) + list(older_orders.keys())):
                recent = recent_orders.get(partner_id, 0)
                older = older_orders.get(partner_id, 0)
                
                if recent > older:
                    increasing_engagement += 1
                elif recent < older:
                    decreasing_engagement += 1
                else:
                    steady_engagement += 1
            
            # Format output
            result = f"\n\nAnalisis Perilaku Pelanggan ({analysis_period} Bulan Terakhir):\n\n"
            
            # Overall metrics
            result += "Metrik Umum:\n"
            result += f"- Total Pelanggan Aktif: {total_customers}\n"
            result += f"- Total Order: {total_orders}\n"
            result += f"- Total Revenue: {total_revenue:,.2f}\n"
            result += f"- Rata-rata Order per Pelanggan: {avg_orders_per_customer:.2f}\n"
            result += f"- Rata-rata Nilai Order: {avg_order_value:,.2f}\n"
            result += f"- Rata-rata Revenue per Pelanggan: {avg_revenue_per_customer:,.2f}\n\n"
            
            # Frequency segmentation
            result += "Segmentasi berdasarkan Frekuensi Order:\n"
            for orders, count in order_frequency.items():
                if count > 0:
                    percentage = (count / total_customers) * 100
                    label = f"{orders} order" if orders != '11+' else "11+ order"
                    result += f"- {label}: {count} pelanggan ({percentage:.2f}%)\n"
            
            # Revenue segmentation
            result += "\nSegmentasi berdasarkan Nilai Transaksi:\n"
            for segment in revenue_segments:
                if segment['count'] > 0:
                    percentage = (segment['count'] / total_customers) * 100
                    result += f"- {segment['name']}: {segment['count']} pelanggan ({percentage:.2f}%)\n"
            
            # Engagement trends
            result += "\nTren Keterlibatan Pelanggan:\n"
            result += f"- Meningkat: {increasing_engagement} pelanggan ({increasing_engagement/total_customers*100:.2f}%)\n"
            result += f"- Menurun: {decreasing_engagement} pelanggan ({decreasing_engagement/total_customers*100:.2f}%)\n"
            result += f"- Stabil: {steady_engagement} pelanggan ({steady_engagement/total_customers*100:.2f}%)\n"
            
            # For car service businesses, analyze car types
            if hasattr(self.env['sale.order'], 'partner_car_id'):
                car_types_query = """
                    SELECT 
                        pc.brand_type as car_type, 
                        COUNT(DISTINCT so.partner_id) as customer_count,
                        COUNT(so.id) as order_count,
                        SUM(so.amount_total) as total_revenue
                    FROM 
                        sale_order so
                    JOIN 
                        res_partner_car pc ON so.partner_car_id = pc.id
                    WHERE 
                        so.date_order >= %s AND
                        so.state in ('sale', 'done')
                    GROUP BY 
                        pc.brand_type
                    ORDER BY 
                        total_revenue DESC
                """
                
                self.env.cr.execute(car_types_query, (start_date,))
                car_type_data = self.env.cr.dictfetchall()
                
                if car_type_data:
                    result += "\nAnalisis berdasarkan Tipe Mobil:\n"
                    for data in car_type_data:
                        car_type = data['car_type'] or "Tidak didefinisikan"
                        cust_count = data['customer_count']
                        order_count = data['order_count']
                        revenue = data['total_revenue']
                        
                        result += f"- {car_type}:\n"
                        result += f"  * Jumlah Pelanggan: {cust_count}\n"
                        result += f"  * Jumlah Order: {order_count}\n"
                        result += f"  * Total Revenue: {revenue:,.2f}\n"
                        result += f"  * Rata-rata Revenue per Pelanggan: {revenue/cust_count:,.2f}\n"
            
            # Find top returning customers
            top_customers_query = """
                SELECT 
                    p.name as customer_name,
                    COUNT(so.id) as order_count,
                    SUM(so.amount_total) as total_spending
                FROM 
                    sale_order so
                JOIN 
                    res_partner p ON so.partner_id = p.id
                WHERE 
                    so.date_order >= %s AND
                    so.state in ('sale', 'done')
                GROUP BY 
                    p.name
                HAVING 
                    COUNT(so.id) > 1
                ORDER BY 
                    order_count DESC, total_spending DESC
                LIMIT 5
            """
            
            self.env.cr.execute(top_customers_query, (start_date,))
            top_customer_data = self.env.cr.dictfetchall()
            
            if top_customer_data:
                result += "\nTop 5 Pelanggan berdasarkan Frekuensi:\n"
                for i, data in enumerate(top_customer_data, 1):
                    result += f"{i}. {data['customer_name']}: {data['order_count']} order, Total: {data['total_spending']:,.2f}\n"
            
            # Identify potential churning customers (previous customers who haven't returned)
            churn_query = """
                SELECT 
                    p.name as customer_name,
                    MAX(so.date_order) as last_order_date,
                    COUNT(so.id) as lifetime_orders,
                    SUM(so.amount_total) as lifetime_value
                FROM 
                    sale_order so
                JOIN 
                    res_partner p ON so.partner_id = p.id
                WHERE 
                    so.state in ('sale', 'done') AND
                    so.date_order < %s AND
                    so.date_order >= %s AND
                    so.partner_id NOT IN (
                        SELECT partner_id FROM sale_order 
                        WHERE date_order >= %s AND state in ('sale', 'done')
                    )
                GROUP BY 
                    p.name
                ORDER BY 
                    lifetime_value DESC
                LIMIT 5
            """
            
            churn_threshold = today - timedelta(days=90)  # Customers who haven't ordered in 3 months
            older_threshold = today - timedelta(days=365)  # Look at 1 year of history
            
            self.env.cr.execute(churn_query, (churn_threshold, older_threshold, churn_threshold))
            churn_data = self.env.cr.dictfetchall()
            
            if churn_data:
                result += "\nPelanggan Berpotensi Churn (Tidak Order > 3 Bulan):\n"
                for i, data in enumerate(churn_data, 1):
                    last_order = data['last_order_date'].strftime('%d %B %Y')
                    result += f"{i}. {data['customer_name']}: Terakhir order {last_order}, " \
                            f"Total historis: {data['lifetime_orders']} order ({data['lifetime_value']:,.2f})\n"
            
            # Add insights and recommendations
            result += "\nInsight & Rekomendasi:\n"
            
            # Insights based on frequency
            one_time_customers = order_frequency.get(1, 0)
            one_time_pct = (one_time_customers / total_customers) * 100 if total_customers else 0
            
            if one_time_pct > 50:
                result += "- Tingginya persentase pelanggan satu kali (one-time) mengindikasikan masalah retensi. " \
                        "Pertimbangkan program loyalty dan follow-up post-service.\n"
            
            # Insights based on revenue
            premium_count = revenue_segments[3]['count']
            premium_pct = (premium_count / total_customers) * 100 if total_customers else 0
            
            if premium_pct > 20:
                result += "- Memiliki basis pelanggan premium yang kuat. Pertimbangkan layanan VIP atau membership khusus.\n"
            
            # Insights based on churn
            if churn_data and len(churn_data) > 3:
                result += "- Beberapa pelanggan bernilai tinggi belum kembali > 3 bulan. " \
                        "Lakukan outreach dengan penawaran khusus untuk re-aktivasi.\n"
            
            # Insights based on engagement trends
            if decreasing_engagement > increasing_engagement:
                result += "- Tren penurunan keterlibatan pelanggan terdeteksi. " \
                        "Evaluasi kualitas layanan dan pengalaman pelanggan.\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error in customer behavior analysis: {str(e)}")
            return f"Error saat menganalisis perilaku pelanggan: {str(e)}"
        
    def _get_business_opportunity_analysis(self, message):
        """Analyze business opportunities based on historical data"""
        try:
            # Validasi bahwa ini adalah query peluang bisnis
            opportunity_keywords = ['opportunity', 'potential', 'peluang', 'potensi', 'growth', 'pertumbuhan', 'recommendation', 'rekomendasi']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in opportunity_keywords):
                return None
                
            # Periode analisis
            today = fields.Date.today()
            analysis_period = 12  # 12 bulan
            start_date = today - timedelta(days=30*analysis_period)
            
            result = "\n\nAnalisis Peluang Bisnis:\n\n"
            
            # 1. Analisis produk/layanan dengan pertumbuhan tertinggi
            growth_query = """
                WITH monthly_sales AS (
                    SELECT
                        p.id as product_id,
                        p.name as product_name,
                        DATE_TRUNC('month', so.date_order)::date as month,
                        SUM(sol.product_uom_qty) as quantity,
                        SUM(sol.price_subtotal) as revenue
                    FROM
                        sale_order_line sol
                    JOIN
                        sale_order so ON sol.order_id = so.id
                    JOIN
                        product_product p ON sol.product_id = p.id
                    WHERE
                        so.date_order >= %s AND
                        so.date_order <= %s AND
                        so.state in ('sale', 'done')
                    GROUP BY
                        p.id, p.name, DATE_TRUNC('month', so.date_order)
                    ORDER BY
                        month
                ),
                product_growth AS (
                    SELECT
                        product_id,
                        product_name,
                        SUM(CASE WHEN month >= %s THEN revenue ELSE 0 END) as recent_revenue,
                        SUM(CASE WHEN month < %s THEN revenue ELSE 0 END) as older_revenue
                    FROM
                        monthly_sales
                    GROUP BY
                        product_id, product_name
                    HAVING
                        SUM(CASE WHEN month < %s THEN revenue ELSE 0 END) > 0
                )
                SELECT
                    product_name,
                    recent_revenue,
                    older_revenue,
                    ((recent_revenue - older_revenue) / older_revenue) * 100 as growth_rate
                FROM
                    product_growth
                WHERE
                    recent_revenue > 0 AND older_revenue > 0
                ORDER BY
                    growth_rate DESC
                LIMIT 5
            """
            
            # Splitting the analysis period in two for comparison
            mid_date = today - timedelta(days=30*analysis_period/2)
            
            self.env.cr.execute(growth_query, (start_date, today, mid_date, mid_date, mid_date))
            growth_data = self.env.cr.dictfetchall()
            
            if growth_data:
                result += "Produk/Layanan dengan Pertumbuhan Tertinggi:\n"
                for i, data in enumerate(growth_data, 1):
                    result += f"{i}. {data['product_name']}\n"
                    result += f"   Pertumbuhan: {data['growth_rate']:.2f}%\n"
                    result += f"   Revenue Terkini: {data['recent_revenue']:,.2f}\n"
            
            # 2. Analisis segmen pelanggan yang berkembang
            segment_query = """
                WITH customer_periods AS (
                    SELECT
                        p.id as partner_id,
                        p.name as partner_name,
                        SUM(CASE WHEN so.date_order >= %s THEN so.amount_total ELSE 0 END) as recent_revenue,
                        SUM(CASE WHEN so.date_order < %s AND so.date_order >= %s THEN so.amount_total ELSE 0 END) as older_revenue,
                        COUNT(DISTINCT CASE WHEN so.date_order >= %s THEN so.id END) as recent_orders,
                        COUNT(DISTINCT CASE WHEN so.date_order < %s AND so.date_order >= %s THEN so.id END) as older_orders
                    FROM
                        sale_order so
                    JOIN
                        res_partner p ON so.partner_id = p.id
                    WHERE
                        so.date_order >= %s AND
                        so.state in ('sale', 'done')
                    GROUP BY
                        p.id, p.name
                )
                SELECT
                    CASE
                        WHEN recent_revenue > 10000000 THEN 'Premium'
                        WHEN recent_revenue > 5000000 THEN 'High Value'
                        WHEN recent_revenue > 1000000 THEN 'Medium Value'
                        ELSE 'Low Value'
                    END as segment,
                    COUNT(*) as customer_count,
                    SUM(recent_revenue) as total_recent_revenue,
                    SUM(older_revenue) as total_older_revenue,
                    CASE 
                        WHEN SUM(older_revenue) > 0 
                        THEN ((SUM(recent_revenue) - SUM(older_revenue)) / SUM(older_revenue)) * 100 
                        ELSE 0 
                    END as growth_rate
                FROM
                    customer_periods
                WHERE
                    recent_revenue > 0 OR older_revenue > 0
                GROUP BY
                    segment
                ORDER BY
                    growth_rate DESC
            """
            
            self.env.cr.execute(segment_query, (
                mid_date, mid_date, start_date, 
                mid_date, mid_date, start_date, 
                start_date
            ))
            segment_data = self.env.cr.dictfetchall()
            
            if segment_data:
                result += "\nSegmen Pelanggan Berdasarkan Pertumbuhan:\n"
                for data in segment_data:
                    growth = data['growth_rate'] or 0
                    growth_trend = "▲" if growth > 0 else "▼" if growth < 0 else "■"
                    result += f"- {data['segment']}: {growth_trend} {abs(growth):.2f}%\n"
                    result += f"  Jumlah Pelanggan: {data['customer_count']}\n"
                    result += f"  Revenue: {data['total_recent_revenue']:,.2f}\n"
            
            # 3. Analisis waktu servis yang sering dibooking
            if hasattr(self.env, 'pitcar.service.booking'):
                time_slot_query = """
                    SELECT
                        FLOOR(booking_time) as hour_slot,
                        COUNT(*) as booking_count
                    FROM
                        pitcar_service_booking
                    WHERE
                        create_date >= %s AND
                        state = 'confirmed'
                    GROUP BY
                        FLOOR(booking_time)
                    ORDER BY
                        booking_count DESC
                """
                
                self.env.cr.execute(time_slot_query, (start_date,))
                time_slot_data = self.env.cr.dictfetchall()
                
                if time_slot_data:
                    result += "\nSlot Waktu Terpopuler untuk Booking:\n"
                    for data in time_slot_data[:3]:  # Top 3
                        hour = int(data['hour_slot'])
                        hour_display = f"{hour:02d}:00 - {hour+1:02d}:00"
                        result += f"- {hour_display}: {data['booking_count']} booking\n"
                    
                    # Identify potential new time slots
                    low_slot_data = sorted(time_slot_data, key=lambda x: x['booking_count'])[:3]
                    
                    result += "\nSlot Waktu dengan Booking Terendah:\n"
                    for data in low_slot_data:
                        hour = int(data['hour_slot'])
                        hour_display = f"{hour:02d}:00 - {hour+1:02d}:00"
                        result += f"- {hour_display}: {data['booking_count']} booking\n"
            
            # 4. Analisis jenis service yang berpotensial untuk dikembangkan
            service_category_query = """
                SELECT
                    so.service_subcategory,
                    COUNT(*) as order_count,
                    SUM(so.amount_total) as total_revenue,
                    COUNT(DISTINCT so.partner_id) as customer_count,
                    SUM(so.amount_total) / COUNT(*) as average_order_value
                FROM
                    sale_order so
                WHERE
                    so.date_order >= %s AND
                    so.state in ('sale', 'done') AND
                    so.service_subcategory IS NOT NULL
                GROUP BY
                    so.service_subcategory
                ORDER BY
                    average_order_value DESC
            """
            
            self.env.cr.execute(service_category_query, (start_date,))
            service_category_data = self.env.cr.dictfetchall()
            
            if service_category_data:
                result += "\nJenis Servis Berdasarkan Nilai Rata-rata:\n"
                for data in service_category_data:
                    subcategory = data['service_subcategory'] or "Tidak Terdefinisi"
                    # Convert service_subcategory code to readable name if available
                    if hasattr(self.env['sale.order'], '_fields') and 'service_subcategory' in self.env['sale.order']._fields:
                        field_def = self.env['sale.order']._fields['service_subcategory']
                        if hasattr(field_def, 'selection'):
                            selection_dict = dict(field_def.selection)
                            subcategory = selection_dict.get(subcategory, subcategory)
                            
                    result += f"- {subcategory}:\n"
                    result += f"  * Order: {data['order_count']}\n"
                    result += f"  * Customer: {data['customer_count']}\n"
                    result += f"  * Revenue: {data['total_revenue']:,.2f}\n"
                    result += f"  * Nilai Rata-rata: {data['average_order_value']:,.2f}\n"
            
            # 5. Analisis potensi cross-selling berdasarkan produk yang sering dibeli bersamaan
            if hasattr(self.env, 'sale.order.line'):
                cross_sell_query = """
                    WITH order_products AS (
                        SELECT
                            so.id as order_id,
                            sol.product_id
                        FROM
                            sale_order so
                        JOIN
                            sale_order_line sol ON so.id = sol.order_id
                        WHERE
                            so.date_order >= %s AND
                            so.state in ('sale', 'done') AND
                            sol.product_id IS NOT NULL
                        GROUP BY
                            so.id, sol.product_id
                    ),
                    product_pairs AS (
                        SELECT
                            a.product_id as product1_id,
                            b.product_id as product2_id,
                            COUNT(*) as pair_count
                        FROM
                            order_products a
                        JOIN
                            order_products b ON a.order_id = b.order_id AND a.product_id < b.product_id
                        GROUP BY
                            a.product_id, b.product_id
                        HAVING
                            COUNT(*) > 1
                        ORDER BY
                            COUNT(*) DESC
                        LIMIT 5
                    )
                    SELECT
                        pp.pair_count,
                        p1.name as product1_name,
                        p2.name as product2_name
                    FROM
                        product_pairs pp
                    JOIN
                        product_product p1 ON pp.product1_id = p1.id
                    JOIN
                        product_product p2 ON pp.product2_id = p2.id
                """
                
                self.env.cr.execute(cross_sell_query, (start_date,))
                cross_sell_data = self.env.cr.dictfetchall()
                
                if cross_sell_data:
                    result += "\nPeluang Cross-Selling (Produk Sering Dibeli Bersama):\n"
                    for i, data in enumerate(cross_sell_data, 1):
                        result += f"{i}. {data['product1_name']} + {data['product2_name']}\n"
                        result += f"   Frekuensi: {data['pair_count']} kali\n"
            
            # 6. Rekomendasi untuk meningkatkan bisnis
            result += "\nRekomendasi Peningkatan Bisnis:\n"
            
            # Berdasarkan produk dengan pertumbuhan tertinggi
            if growth_data and len(growth_data) > 0:
                top_growth_product = growth_data[0]['product_name']
                result += f"1. Fokus pengembangan pada produk/layanan '{top_growth_product}' yang menunjukkan pertumbuhan tertinggi.\n"
            
            # Berdasarkan segmen pelanggan
            if segment_data:
                growing_segments = [d for d in segment_data if d.get('growth_rate', 0) > 0]
                if growing_segments:
                    top_segment = growing_segments[0]['segment']
                    result += f"2. Kembangkan program khusus untuk segmen '{top_segment}' yang menunjukkan pertumbuhan signifikan.\n"
            
            # Berdasarkan waktu booking
            if 'time_slot_data' in locals() and time_slot_data:
                # Rekomendasi untuk slot waktu kurang populer
                if low_slot_data:
                    low_hour = int(low_slot_data[0]['hour_slot'])
                    low_hour_display = f"{low_hour:02d}:00 - {low_hour+1:02d}:00"
                    result += f"3. Pertimbangkan promo khusus untuk slot waktu {low_hour_display} untuk mengoptimalkan kapasitas.\n"
            
            # Berdasarkan cross-selling
            if 'cross_sell_data' in locals() and cross_sell_data:
                result += "4. Implementasikan strategi cross-selling berdasarkan produk yang sering dibeli bersama.\n"
            
            # Rekomendasi umum
            result += "5. Lakukan program retensi untuk pelanggan dengan penurunan frekuensi kunjungan.\n"
            result += "6. Evaluasi kembali harga untuk layanan dengan nilai rata-rata rendah tetapi volume tinggi.\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error in business opportunity analysis: {str(e)}")
            return f"Error saat menganalisis peluang bisnis: {str(e)}"
        
    def _get_workflow_efficiency_analysis(self, message):
        """Analyze workshop workflow efficiency and mechanic performance"""
        try:
            workflow_keywords = ['workflow', 'efficiency', 'process', 'bottleneck', 'mechanic performance', 
                                'efisiensi', 'proses', 'kinerja mekanik', 'lead time', 'waktu tunggu']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in workflow_keywords):
                return None
                
            # Tentukan periode analisis
            today = fields.Date.today()
            analysis_period = 3  # Default: 3 bulan
            
            # Jika disebutkan dalam pesan, gunakan nilai yang disebutkan
            for i in range(1, 13):
                if f"{i} bulan" in message_lower or f"{i} month" in message_lower:
                    analysis_period = i
                    break
            
            start_date = today - timedelta(days=30*analysis_period)
            
            result = f"\n\nAnalisis Efisiensi Workflow ({analysis_period} Bulan Terakhir):\n\n"
            
            # 1. Analisis Lead Time Keseluruhan
            lead_time_query = """
                SELECT
                    AVG(lead_time_servis) as avg_lead_time,
                    MIN(lead_time_servis) as min_lead_time,
                    MAX(lead_time_servis) as max_lead_time,
                    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY lead_time_servis) as median_lead_time,
                    STDDEV(lead_time_servis) as std_lead_time
                FROM
                    sale_order
                WHERE
                    controller_selesai >= %s AND
                    controller_selesai <= %s AND
                    controller_mulai_servis IS NOT NULL AND
                    controller_selesai IS NOT NULL AND
                    lead_time_servis > 0
            """
            
            self.env.cr.execute(lead_time_query, (start_date, today))
            lead_time_data = self.env.cr.dictfetchall()
            
            if lead_time_data and lead_time_data[0]['avg_lead_time']:
                data = lead_time_data[0]
                result += "Lead Time Servis:\n"
                result += f"- Rata-rata: {data['avg_lead_time']:.2f} jam\n"
                result += f"- Median: {data['median_lead_time']:.2f} jam\n"
                result += f"- Minimum: {data['min_lead_time']:.2f} jam\n"
                result += f"- Maksimum: {data['max_lead_time']:.2f} jam\n"
                result += f"- Standar Deviasi: {data['std_lead_time']:.2f} jam\n"
            
            # 2. Analisis berdasarkan jenis servis
            service_lead_time_query = """
                SELECT
                    service_subcategory,
                    COUNT(*) as service_count,
                    AVG(lead_time_servis) as avg_lead_time,
                    MIN(lead_time_servis) as min_lead_time,
                    MAX(lead_time_servis) as max_lead_time
                FROM
                    sale_order
                WHERE
                    controller_selesai >= %s AND
                    controller_selesai <= %s AND
                    controller_mulai_servis IS NOT NULL AND
                    controller_selesai IS NOT NULL AND
                    lead_time_servis > 0 AND
                    service_subcategory IS NOT NULL
                GROUP BY
                    service_subcategory
                ORDER BY
                    service_count DESC
            """
            
            self.env.cr.execute(service_lead_time_query, (start_date, today))
            service_lead_time_data = self.env.cr.dictfetchall()
            
            if service_lead_time_data:
                result += "\nLead Time berdasarkan Jenis Servis:\n"
                for data in service_lead_time_data:
                    subcategory = data['service_subcategory'] or "Tidak Terdefinisi"
                    # Convert service_subcategory code to readable name if available
                    if hasattr(self.env['sale.order'], '_fields') and 'service_subcategory' in self.env['sale.order']._fields:
                        field_def = self.env['sale.order']._fields['service_subcategory']
                        if hasattr(field_def, 'selection'):
                            selection_dict = dict(field_def.selection)
                            subcategory = selection_dict.get(subcategory, subcategory)
                            
                    result += f"- {subcategory} ({data['service_count']} order):\n"
                    result += f"  * Rata-rata: {data['avg_lead_time']:.2f} jam\n"
                    result += f"  * Minimum: {data['min_lead_time']:.2f} jam\n"
                    result += f"  * Maksimum: {data['max_lead_time']:.2f} jam\n"
            
            # 3. Analisis Job Stop (waktu tunggu)
            job_stop_query = """
                SELECT
                    AVG(lead_time_tunggu_konfirmasi) as avg_confirmation_wait,
                    AVG(lead_time_tunggu_part1) as avg_part1_wait,
                    AVG(lead_time_tunggu_part2) as avg_part2_wait,
                    AVG(lead_time_tunggu_sublet) as avg_sublet_wait
                FROM
                    sale_order
                WHERE
                    controller_selesai >= %s AND
                    controller_selesai <= %s AND
                    controller_mulai_servis IS NOT NULL AND
                    controller_selesai IS NOT NULL
            """
            
            self.env.cr.execute(job_stop_query, (start_date, today))
            job_stop_data = self.env.cr.dictfetchall()
            
            if job_stop_data:
                data = job_stop_data[0]
                result += "\nAnalisis Waktu Tunggu (Job Stop):\n"
                result += f"- Tunggu Konfirmasi: {data['avg_confirmation_wait'] or 0:.2f} jam\n"
                result += f"- Tunggu Part 1: {data['avg_part1_wait'] or 0:.2f} jam\n"
                result += f"- Tunggu Part 2: {data['avg_part2_wait'] or 0:.2f} jam\n"
                result += f"- Tunggu Sublet: {data['avg_sublet_wait'] or 0:.2f} jam\n"
                
                # Cari job stop yang paling berkontribusi pada keterlambatan
                job_stops = [
                    ('Tunggu Konfirmasi', data['avg_confirmation_wait'] or 0),
                    ('Tunggu Part 1', data['avg_part1_wait'] or 0),
                    ('Tunggu Part 2', data['avg_part2_wait'] or 0),
                    ('Tunggu Sublet', data['avg_sublet_wait'] or 0)
                ]
                
                job_stops_sorted = sorted(job_stops, key=lambda x: x[1], reverse=True)
                
                if job_stops_sorted[0][1] > 0:
                    result += f"\nBottleneck utama: {job_stops_sorted[0][0]} ({job_stops_sorted[0][1]:.2f} jam)\n"
            
            # 4. Analisis Kinerja Mekanik
            mechanic_query = """
                WITH mechanic_services AS (
                    SELECT
                        m.id as mechanic_id,
                        m.name as mechanic_name,
                        COUNT(so.id) as service_count,
                        AVG(so.lead_time_servis) as avg_lead_time,
                        AVG(so.service_time_efficiency) as avg_efficiency,
                        AVG(CASE WHEN so.is_on_time THEN 1.0 ELSE 0.0 END) as on_time_rate
                    FROM
                        sale_order so
                    JOIN
                        pitcar_mechanic_new_sale_order_rel rel ON so.id = rel.sale_order_id
                    JOIN
                        pitcar_mechanic_new m ON rel.pitcar_mechanic_new_id = m.id
                    WHERE
                        so.controller_selesai >= %s AND
                        so.controller_selesai <= %s AND
                        so.controller_mulai_servis IS NOT NULL AND
                        so.controller_selesai IS NOT NULL AND
                        so.lead_time_servis > 0
                    GROUP BY
                        m.id, m.name
                    HAVING
                        COUNT(so.id) > 5  -- Minimal 5 service untuk mendapatkan data yang signifikan
                )
                SELECT *
                FROM mechanic_services
                ORDER BY avg_efficiency DESC
            """
            
            self.env.cr.execute(mechanic_query, (start_date, today))
            mechanic_data = self.env.cr.dictfetchall()
            
            if mechanic_data:
                result += "\nKinerja Mekanik (Efisiensi):\n"
                for i, data in enumerate(mechanic_data, 1):
                    efficiency = data['avg_efficiency'] or 0
                    on_time = data['on_time_rate'] or 0
                    
                    result += f"{i}. {data['mechanic_name']}:\n"
                    result += f"   * Jumlah Servis: {data['service_count']}\n"
                    result += f"   * Efisiensi Waktu: {efficiency:.2f}%\n"
                    result += f"   * On-Time Rate: {on_time*100:.2f}%\n"
                    result += f"   * Lead Time Rata-rata: {data['avg_lead_time']:.2f} jam\n"
                
                # Identify top and bottom performers
                if len(mechanic_data) >= 3:
                    top_performer = mechanic_data[0]
                    bottom_performer = mechanic_data[-1]
                    
                    efficiency_gap = (top_performer['avg_efficiency'] or 0) - (bottom_performer['avg_efficiency'] or 0)
                    
                    if efficiency_gap > 20:  # Significant gap
                        result += f"\nGap efisiensi antara mekanik terbaik dan terburuk: {efficiency_gap:.2f}%\n"
            
            # 5. Tren efisiensi dari waktu ke waktu
            efficiency_trend_query = """
                SELECT
                    DATE_TRUNC('month', controller_selesai)::date as month,
                    COUNT(*) as service_count,
                    AVG(lead_time_servis) as avg_lead_time,
                    AVG(service_time_efficiency) as avg_efficiency,
                    AVG(CASE WHEN is_on_time THEN 1.0 ELSE 0.0 END) as on_time_rate
                FROM
                    sale_order
                WHERE
                    controller_selesai >= %s AND
                    controller_selesai <= %s AND
                    controller_mulai_servis IS NOT NULL AND
                    controller_selesai IS NOT NULL AND
                    lead_time_servis > 0
                GROUP BY
                    DATE_TRUNC('month', controller_selesai)
                ORDER BY
                    month
            """
            
            self.env.cr.execute(efficiency_trend_query, (start_date, today))
            efficiency_trend_data = self.env.cr.dictfetchall()
            
            if efficiency_trend_data and len(efficiency_trend_data) > 1:
                result += "\nTren Efisiensi (Bulanan):\n"
                
                # Menghitung tren dengan simple linear regression
                months = []
                efficiencies = []
                on_time_rates = []
                
                for i, data in enumerate(efficiency_trend_data):
                    month_name = datetime.strptime(str(data['month']), '%Y-%m-%d').strftime('%b %Y')
                    efficiency = data['avg_efficiency'] or 0
                    on_time = (data['on_time_rate'] or 0) * 100
                    
                    result += f"- {month_name}: Efisiensi {efficiency:.2f}%, On-Time {on_time:.2f}%\n"
                    
                    months.append(i)
                    efficiencies.append(efficiency)
                    on_time_rates.append(on_time)
                
                # Hitung tren menggunakan linear regression sederhana
                if len(months) > 1:
                    try:
                        import numpy as np
                        from scipy import stats
                        
                        efficiency_slope, _, _, _, _ = stats.linregress(months, efficiencies)
                        on_time_slope, _, _, _, _ = stats.linregress(months, on_time_rates)
                        
                        efficiency_trend = "meningkat" if efficiency_slope > 0 else "menurun"
                        on_time_trend = "meningkat" if on_time_slope > 0 else "menurun"
                        
                        result += f"\nTren Efisiensi: {efficiency_trend} ({abs(efficiency_slope):.2f}% per bulan)\n"
                        result += f"Tren On-Time: {on_time_trend} ({abs(on_time_slope):.2f}% per bulan)\n"
                        
                    except (ImportError, Exception) as e:
                        # If numpy/scipy not available or error in calculation
                        _logger.warning(f"Could not calculate trends: {str(e)}")
                        
                        # Simple trend calculation
                        first_efficiency = efficiencies[0]
                        last_efficiency = efficiencies[-1]
                        
                        if last_efficiency > first_efficiency:
                            result += "\nTren Efisiensi: meningkat\n"
                        else:
                            result += "\nTren Efisiensi: menurun\n"
            
            # 6. Rekomendasi Peningkatan Efisiensi
            result += "\nRekomendasi Peningkatan Efisiensi:\n"
            
            if 'job_stops_sorted' in locals() and job_stops_sorted[0][1] > 1.0:
                # Jika ada bottleneck signifikan
                bottleneck = job_stops_sorted[0][0]
                if bottleneck == 'Tunggu Konfirmasi':
                    result += "1. Perbaiki proses konfirmasi dengan pelanggan. Pertimbangkan penggunaan sistem notifikasi otomatis atau WhatsApp Business API.\n"
                elif bottleneck in ['Tunggu Part 1', 'Tunggu Part 2']:
                    result += "1. Tingkatkan manajemen inventori untuk part-part yang sering digunakan. Implementasikan sistem level stok minimum.\n"
                elif bottleneck == 'Tunggu Sublet':
                    result += "1. Evaluasi kembali proses dan vendor sublet. Pertimbangkan untuk menambah vendor alternatif.\n"
            
            if 'efficiency_gap' in locals() and efficiency_gap > 20:
                result += "2. Lakukan knowledge sharing dan mentoring antara mekanik dengan efisiensi tertinggi kepada mekanik dengan performa rendah.\n"
            
            if 'service_lead_time_data' in locals() and service_lead_time_data:
                # Find service types with high variance
                high_variance_services = [
                    s for s in service_lead_time_data 
                    if (s['max_lead_time'] - s['min_lead_time']) / s['avg_lead_time'] > 1.5
                ]
                
                if high_variance_services:
                    service = high_variance_services[0]['service_subcategory']
                    if hasattr(self.env['sale.order'], '_fields') and 'service_subcategory' in self.env['sale.order']._fields:
                        field_def = self.env['sale.order']._fields['service_subcategory']
                        if hasattr(field_def, 'selection'):
                            selection_dict = dict(field_def.selection)
                            service = selection_dict.get(service, service)
                            
                    result += f"3. Standardisasi proses untuk jenis servis '{service}' yang memiliki variasi waktu tinggi.\n"
            
            if 'efficiency_trend' in locals() and efficiency_trend == 'menurun':
                result += "4. Lakukan refreshment training dan evaluasi peralatan bengkel untuk mengatasi tren penurunan efisiensi.\n"
            
            # General recommendations
            result += "5. Implementasikan atau tingkatkan standar waktu (flat rate) untuk setiap tipe pekerjaan.\n"
            result += "6. Gunakan analisis beban kerja untuk penjadwalan mekanik yang lebih optimal.\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error in workflow efficiency analysis: {str(e)}")
            return f"Error saat menganalisis efisiensi workflow: {str(e)}"
        
    def _get_rfm_analysis(self, message):
        """Analyze customer data using RFM (Recency, Frequency, Monetary) method"""
        try:
            # Validasi bahwa ini adalah query RFM
            rfm_keywords = ['rfm', 'recency', 'frequency', 'monetary', 'segmentasi', 'segmentation', 'customer', 'pelanggan']
            message_lower = message.lower()
            
            if not any(keyword in message_lower for keyword in rfm_keywords):
                return None
                
            # Tentukan rentang waktu analisis
            today = fields.Date.today()
            analysis_period = 12  # Default: 12 bulan
            
            # Jika disebutkan dalam pesan, gunakan nilai yang disebutkan
            for i in range(1, 37):  # Support hingga 3 tahun
                if f"{i} bulan" in message_lower or f"{i} month" in message_lower:
                    analysis_period = i
                    break
            
            start_date = today - timedelta(days=30*analysis_period)
            
            # Query untuk mendapatkan data RFM
            rfm_query = """
                WITH customer_data AS (
                    SELECT
                        so.partner_id,
                        MAX(so.date_order) as last_order_date,
                        COUNT(so.id) as order_count,
                        SUM(so.amount_total) as total_spending
                    FROM
                        sale_order so
                    WHERE
                        so.date_order >= %s AND
                        so.date_order <= %s AND
                        so.state in ('sale', 'done') AND
                        so.partner_id IS NOT NULL
                    GROUP BY
                        so.partner_id
                ),
                rfm_scores AS (
                    SELECT
                        cd.partner_id,
                        NTILE(5) OVER (ORDER BY cd.last_order_date DESC) as recency_score,
                        NTILE(5) OVER (ORDER BY cd.order_count ASC) as frequency_score,
                        NTILE(5) OVER (ORDER BY cd.total_spending ASC) as monetary_score
                    FROM
                        customer_data cd
                ),
                rfm_segments AS (
                    SELECT
                        rs.partner_id,
                        6-rs.recency_score as r_score,  -- Invert so 5 is best (most recent)
                        rs.frequency_score as f_score,
                        rs.monetary_score as m_score,
                        (6-rs.recency_score) * 100 + rs.frequency_score * 10 + rs.monetary_score as rfm_combined
                    FROM
                        rfm_scores rs
                )
                SELECT
                    p.id as partner_id,
                    p.name as partner_name,
                    rs.r_score,
                    rs.f_score,
                    rs.m_score,
                    rs.rfm_combined,
                    CASE
                        WHEN rs.r_score >= 4 AND rs.f_score >= 4 AND rs.m_score >= 4 THEN 'Champions'
                        WHEN rs.r_score >= 2 AND rs.f_score >= 3 AND rs.m_score >= 3 THEN 'Loyal Customers'
                        WHEN rs.r_score >= 3 AND rs.f_score >= 1 AND rs.m_score >= 1 THEN 'Potential Loyalist'
                        WHEN rs.r_score >= 4 AND rs.f_score <= 1 AND rs.m_score <= 1 THEN 'New Customers'
                        WHEN rs.r_score >= 3 AND rs.f_score <= 2 AND rs.m_score <= 2 THEN 'Promising'
                        WHEN rs.r_score >= 2 AND rs.f_score <= 2 AND rs.m_score <= 2 THEN 'Customers Needing Attention'
                        WHEN rs.r_score <= 2 AND rs.f_score >= 2 AND rs.m_score >= 2 THEN 'At Risk'
                        WHEN rs.r_score <= 1 AND rs.f_score >= 4 AND rs.m_score >= 4 THEN 'Can\'t Lose Them'
                        WHEN rs.r_score <= 2 AND rs.f_score >= 2 AND rs.m_score <= 2 THEN 'Hibernating'
                        WHEN rs.r_score <= 1 AND rs.f_score <= 2 AND rs.m_score <= 2 THEN 'Lost'
                        ELSE 'Others'
                    END as segment
                FROM
                    rfm_segments rs
                JOIN
                    res_partner p ON rs.partner_id = p.id
                ORDER BY
                    rs.rfm_combined DESC
            """
            
            self.env.cr.execute(rfm_query, (start_date, today))
            rfm_data = self.env.cr.dictfetchall()
            
            if not rfm_data:
                return f"Tidak ditemukan data pelanggan yang cukup untuk analisis RFM dalam {analysis_period} bulan terakhir."
            
            # Kelompokkan berdasarkan segment
            segments = {}
            for data in rfm_data:
                segment = data['segment']
                if segment not in segments:
                    segments[segment] = []
                segments[segment].append(data)
            
            # Format output
            result = f"\n\nAnalisis RFM (Recency, Frequency, Monetary) - {analysis_period} Bulan Terakhir:\n\n"
            
            # Penjelasan sederhana RFM
            result += "Penjelasan Skor RFM:\n"
            result += "- R (Recency): 5 = Baru saja order, 1 = Sudah lama tidak order\n"
            result += "- F (Frequency): 5 = Sering order, 1 = Jarang order\n"
            result += "- M (Monetary): 5 = Nilai order tinggi, 1 = Nilai order rendah\n\n"
            
            # Distribusi segmen
            result += "Distribusi Segmen Pelanggan:\n"
            for segment, customers in sorted(segments.items(), key=lambda x: len(x[1]), reverse=True):
                count = len(customers)
                percentage = (count / len(rfm_data)) * 100
                result += f"- {segment}: {count} pelanggan ({percentage:.2f}%)\n"
            
            # Detail segmen beserta rekomendasi
            result += "\nAnalisis Segmen & Rekomendasi Strategi:\n"
            
            # Champions
            if 'Champions' in segments:
                champion_count = len(segments['Champions'])
                result += f"1. Champions ({champion_count} pelanggan):\n"
                result += "   - Karakteristik: Pelanggan loyal dengan nilai dan frekuensi tinggi, baru saja bertransaksi\n"
                result += "   - Rekomendasi: Pertahankan dengan program rewards, jadikan brand ambassador, minta referensi\n"
            
            # Loyal Customers
            if 'Loyal Customers' in segments:
                loyal_count = len(segments['Loyal Customers'])
                result += f"2. Loyal Customers ({loyal_count} pelanggan):\n"
                result += "   - Karakteristik: Pelanggan setia dengan frekuensi dan nilai transaksi yang baik\n"
                result += "   - Rekomendasi: Program loyalitas, special offers, upsell ke layanan premium\n"
            
            # At Risk
            if 'At Risk' in segments:
                at_risk_count = len(segments['At Risk'])
                result += f"3. At Risk ({at_risk_count} pelanggan):\n"
                result += "   - Karakteristik: Pelanggan bernilai tinggi yang sudah lama tidak bertransaksi\n"
                result += "   - Rekomendasi: Reactivation campaign, reminder service, special offers\n"
            
            # New Customers
            if 'New Customers' in segments:
                new_count = len(segments['New Customers'])
                result += f"4. New Customers ({new_count} pelanggan):\n"
                result += "   - Karakteristik: Pelanggan baru dengan transaksi terbaru namun frekuensi rendah\n"
                result += "   - Rekomendasi: Welcome program, edukasi tentang layanan lain, follow-up kepuasan\n"
            
            # Can't Lose Them
            if 'Can\'t Lose Them' in segments:
                cant_lose_count = len(segments['Can\'t Lose Them'])
                result += f"5. Can't Lose Them ({cant_lose_count} pelanggan):\n"
                result += "   - Karakteristik: Pelanggan high-value yang sudah lama tidak bertransaksi\n"
                result += "   - Rekomendasi: Win-back campaign, personal outreach, survey kepuasan\n"
            
            # Lost
            if 'Lost' in segments:
                lost_count = len(segments['Lost'])
                result += f"6. Lost ({lost_count} pelanggan):\n"
                result += "   - Karakteristik: Pelanggan yang sudah lama tidak bertransaksi dengan nilai rendah\n"
                result += "   - Rekomendasi: Reactivation dengan penawaran khusus atau membiarkan secara natural\n"
            
            # Top 5 customers overall
            top_customers = sorted(rfm_data, key=lambda x: x['rfm_combined'], reverse=True)[:5]
            
            if top_customers:
                result += "\nTop 5 Pelanggan Berdasarkan Skor RFM:\n"
                for i, customer in enumerate(top_customers, 1):
                    result += f"{i}. {customer['partner_name']}\n"
                    result += f"   - Skor RFM: {customer['r_score']}-{customer['f_score']}-{customer['m_score']}\n"
                    result += f"   - Segmen: {customer['segment']}\n"
            
            # Overall strategy recommendations
            result += "\nRekomendasi Strategi Keseluruhan:\n"
            
            # Count segment distribution
            champion_loyal_pct = sum(len(segments.get(s, [])) for s in ['Champions', 'Loyal Customers']) / len(rfm_data) * 100
            at_risk_pct = sum(len(segments.get(s, [])) for s in ['At Risk', 'Can\'t Lose Them', 'Hibernating']) / len(rfm_data) * 100
            new_potential_pct = sum(len(segments.get(s, [])) for s in ['New Customers', 'Potential Loyalist', 'Promising']) / len(rfm_data) * 100
            
            if champion_loyal_pct < 30:
                result += "1. Fokus pada program retensi pelanggan untuk meningkatkan persentase Champion & Loyal Customers.\n"
            
            if at_risk_pct > 25:
                result += "2. Implementasikan win-back campaign untuk segmen At Risk yang memiliki persentase tinggi.\n"
            
            if new_potential_pct > 40:
                result += "3. Maksimalkan konversi pelanggan baru menjadi loyal dengan program follow-up konsisten.\n"
            
            result += "4. Implementasikan program engagement berbeda untuk setiap segmen RFM.\n"
            result += "5. Lakukan evaluasi RFM secara berkala (3-6 bulan sekali) untuk memantau pergeseran segmen.\n"
            
            return result
            
        except Exception as e:
            _logger.error(f"Error in RFM analysis: {str(e)}")
            return f"Error saat melakukan analisis RFM: {str(e)}"

    def _get_customer_data(self, message):
        """Get customer data analysis based on the user's message"""
        try:
            # Analisis periode waktu dari pesan
            time_period = self._extract_time_period(message)
            date_from, date_to = self._get_date_range(time_period)
            
            message_lower = message.lower()
            
            # Determine query type (general overview or specific analysis)
            is_overview = 'overview' in message_lower or 'ringkasan' in message_lower or 'summary' in message_lower
            is_segment_analysis = 'segment' in message_lower or 'segmentasi' in message_lower
            is_car_analysis = any(keyword in message_lower for keyword in ['car', 'mobil', 'kendaraan', 'brand', 'merek'])
            is_acquisition_analysis = any(keyword in message_lower for keyword in ['acquisition', 'source', 'acquisition source', 'sumber', 'sumber akuisisi'])
            is_retention_analysis = any(keyword in message_lower for keyword in ['retention', 'retensi', 'loyal', 'loyalty', 'repeat'])
            
            # Customer overview
            if is_overview or not (is_segment_analysis or is_car_analysis or is_acquisition_analysis or is_retention_analysis):
                result = self._get_customer_overview(date_from, date_to)
            else:
                result = ""
                
            # Customer segmentation
            if is_segment_analysis:
                segment_data = self._get_customer_segmentation(date_from, date_to, message)
                result += "\n\n" + segment_data if result else segment_data
            
            # Car analysis
            if is_car_analysis:
                car_analysis = self._get_customer_car_analysis(date_from, date_to, message)
                result += "\n\n" + car_analysis if result else car_analysis
            
            # Acquisition source analysis
            if is_acquisition_analysis:
                acquisition_data = self._get_customer_acquisition_analysis(date_from, date_to, message)
                result += "\n\n" + acquisition_data if result else acquisition_data
                
            # Retention analysis
            if is_retention_analysis:
                retention_data = self._get_customer_retention_analysis(date_from, date_to, message)
                result += "\n\n" + retention_data if result else retention_data
            
            return result
            
        except Exception as e:
            _logger.error(f"Error getting customer data: {str(e)}")
            return f"\n\nError mendapatkan data customer: {str(e)}"

    def _get_customer_overview(self, date_from, date_to):
        """Get general customer overview"""
        # Query data pelanggan
        customers = self.env['res.partner'].search([
            ('create_date', '>=', date_from),
            ('create_date', '<=', date_to),
            ('customer_rank', '>', 0),
        ])
        
        # Statistik umum
        total_customers = len(customers)
        new_customers = len(customers.filtered(lambda c: c.create_date >= date_from and c.create_date <= date_to))
        
        # Query untuk mendapatkan data kendaraan
        all_cars = self.env['res.partner.car'].search([('partner_id', 'in', customers.ids)])
        customers_with_cars = len(set(all_cars.mapped('partner_id.id')))
        
        # Query untuk mendapatkan data transaksi
        orders = self.env['sale.order'].search([
            ('partner_id', 'in', customers.ids),
            ('date_order', '>=', date_from),
            ('date_order', '<=', date_to),
            ('state', 'in', ['sale', 'done']),
        ])
        
        active_customers = len(set(orders.mapped('partner_id.id')))
        total_revenue = sum(orders.mapped('amount_total'))
        avg_order_value = total_revenue / len(orders) if orders else 0
        
        # Format output
        result = f"""
    Customer Overview ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):

    Statistik Umum:
    - Total Pelanggan: {total_customers}
    - Pelanggan Baru: {new_customers}
    - Pelanggan Aktif (dengan transaksi): {active_customers}
    - Persentase Pelanggan Aktif: {(active_customers/total_customers*100) if total_customers else 0:.2f}%

    Statistik Kendaraan:
    - Total Kendaraan Terdaftar: {len(all_cars)}
    - Rata-rata Kendaraan per Pelanggan: {len(all_cars)/customers_with_cars if customers_with_cars else 0:.2f}
    - Persentase Pelanggan dengan Kendaraan: {(customers_with_cars/total_customers*100) if total_customers else 0:.2f}%

    Transaksi:
    - Total Order: {len(orders)}
    - Total Revenue: {total_revenue:,.2f}
    - Rata-rata Nilai Order: {avg_order_value:,.2f}
    """

        # Analisis demografis (jika ada data gender)
        if any(c.gender for c in customers):
            male_count = len(customers.filtered(lambda c: c.gender == 'male'))
            female_count = len(customers.filtered(lambda c: c.gender == 'female'))
            
            result += f"""
    Demografis:
    - Laki-laki: {male_count} ({(male_count/total_customers*100) if total_customers else 0:.2f}%)
    - Perempuan: {female_count} ({(female_count/total_customers*100) if total_customers else 0:.2f}%)
    """

        # Top customers
        if orders:
            customer_sales = {}
            for order in orders:
                customer_id = order.partner_id.id
                customer_name = order.partner_id.name
                if customer_id in customer_sales:
                    customer_sales[customer_id]['total'] += order.amount_total
                    customer_sales[customer_id]['count'] += 1
                else:
                    customer_sales[customer_id] = {
                        'name': customer_name,
                        'total': order.amount_total,
                        'count': 1
                    }
            
            # Sort by total sales
            top_customers = sorted(customer_sales.items(), key=lambda x: x[1]['total'], reverse=True)[:5]
            
            result += "\nTop 5 Pelanggan Berdasarkan Nilai Transaksi:\n"
            for i, (customer_id, data) in enumerate(top_customers, 1):
                result += f"{i}. {data['name']}: {data['total']:,.2f} ({data['count']} order)\n"
        
        return result

    def _get_customer_segmentation(self, date_from, date_to, message):
        """Analyze customer segmentation based on categories/tags"""
        # Query all customers
        customers = self.env['res.partner'].search([
            ('customer_rank', '>', 0),
        ])
        
        # Analisis berdasarkan kategori (tags)
        categories = self.env['res.partner.category'].search([])
        
        if not categories:
            return "Tidak ditemukan data kategori pelanggan."
        
        result = f"Analisis Segmentasi Pelanggan ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n\n"
        
        # Count by category
        result += "Distribusi Pelanggan Berdasarkan Kategori:\n"
        for category in categories:
            count = len(category.partner_ids.filtered(lambda p: p.customer_rank > 0))
            if count > 0:
                result += f"- {category.name}: {count} pelanggan\n"
        
        # Order analysis by category
        result += "\nAnalisis Transaksi Berdasarkan Kategori:\n"
        for category in categories:
            customer_ids = category.partner_ids.filtered(lambda p: p.customer_rank > 0).ids
            if not customer_ids:
                continue
                
            orders = self.env['sale.order'].search([
                ('partner_id', 'in', customer_ids),
                ('date_order', '>=', date_from),
                ('date_order', '<=', date_to),
                ('state', 'in', ['sale', 'done']),
            ])
            
            if orders:
                total_revenue = sum(orders.mapped('amount_total'))
                avg_order_value = total_revenue / len(orders)
                unique_customers = len(set(orders.mapped('partner_id.id')))
                
                result += f"- {category.name}:\n"
                result += f"  * Orders: {len(orders)}\n"
                result += f"  * Total Revenue: {total_revenue:,.2f}\n"
                result += f"  * Rata-rata Nilai Order: {avg_order_value:,.2f}\n"
                result += f"  * Pelanggan Aktif: {unique_customers} dari {len(category.partner_ids.filtered(lambda p: p.customer_rank > 0))}\n"
        
        # Add custom segmentation based on transaction frequency
        all_orders = self.env['sale.order'].search([
            ('date_order', '>=', date_from),
            ('date_order', '<=', date_to),
            ('state', 'in', ['sale', 'done']),
        ])
        
        customer_frequency = {}
        for order in all_orders:
            customer_id = order.partner_id.id
            if customer_id in customer_frequency:
                customer_frequency[customer_id] += 1
            else:
                customer_frequency[customer_id] = 1
        
        # Define segments
        segments = {
            'One-Time': [c for c, freq in customer_frequency.items() if freq == 1],
            'Occasional (2-3)': [c for c, freq in customer_frequency.items() if 2 <= freq <= 3],
            'Regular (4-6)': [c for c, freq in customer_frequency.items() if 4 <= freq <= 6],
            'Loyal (7+)': [c for c, freq in customer_frequency.items() if freq >= 7],
        }
        
        result += "\nSegmentasi Berdasarkan Frekuensi Transaksi:\n"
        for segment, customer_ids in segments.items():
            if customer_ids:
                segment_revenue = sum(o.amount_total for o in all_orders if o.partner_id.id in customer_ids)
                
                result += f"- {segment}: {len(customer_ids)} pelanggan\n"
                result += f"  * Total Revenue: {segment_revenue:,.2f}\n"
                result += f"  * Rata-rata per Pelanggan: {segment_revenue/len(customer_ids):,.2f}\n"
        
        return result

    def _get_customer_car_analysis(self, date_from, date_to, message):
        """Analyze customer vehicle data"""
        # Get all customer cars
        all_cars = self.env['res.partner.car'].search([])
        
        if not all_cars:
            return "Tidak ditemukan data kendaraan pelanggan."
        
        result = f"Analisis Kendaraan Pelanggan ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n\n"
        
        # Distribution by brand
        brands = self.env['res.partner.car.brand'].search([])
        
        result += "Distribusi Berdasarkan Merek:\n"
        for brand in brands:
            cars = all_cars.filtered(lambda c: c.brand.id == brand.id)
            if cars:
                result += f"- {brand.name}: {len(cars)} kendaraan\n"
        
        # Distribution by year
        years = {}
        for car in all_cars:
            if car.year in years:
                years[car.year] += 1
            else:
                years[car.year] = 1
        
        result += "\nDistribusi Berdasarkan Tahun:\n"
        for year, count in sorted(years.items(), reverse=True):
            result += f"- {year}: {count} kendaraan\n"
        
        # Distribution by engine type
        engine_types = {}
        for car in all_cars:
            if car.engine_type in engine_types:
                engine_types[car.engine_type] += 1
            else:
                engine_types[car.engine_type] = 1
        
        result += "\nDistribusi Berdasarkan Jenis Mesin:\n"
        engine_type_labels = {
            'petrol': 'Bensin',
            'diesel': 'Diesel',
            'electric': 'Listrik',
            'hybrid': 'Hybrid',
            'gas': 'Gas',
            'other': 'Lainnya'
        }
        
        for engine_type, count in sorted(engine_types.items(), key=lambda x: x[1], reverse=True):
            label = engine_type_labels.get(engine_type, engine_type)
            result += f"- {label}: {count} kendaraan\n"
        
        # Transaction analysis by car brand
        result += "\nAnalisis Transaksi Berdasarkan Merek Kendaraan:\n"
        for brand in brands:
            cars = all_cars.filtered(lambda c: c.brand.id == brand.id)
            customer_ids = cars.mapped('partner_id.id')
            
            orders = self.env['sale.order'].search([
                ('partner_id', 'in', customer_ids),
                ('date_order', '>=', date_from),
                ('date_order', '<=', date_to),
                ('state', 'in', ['sale', 'done']),
            ])
            
            if orders:
                total_revenue = sum(orders.mapped('amount_total'))
                avg_order_value = total_revenue / len(orders)
                
                result += f"- {brand.name}:\n"
                result += f"  * Orders: {len(orders)}\n"
                result += f"  * Total Revenue: {total_revenue:,.2f}\n"
                result += f"  * Rata-rata Nilai Order: {avg_order_value:,.2f}\n"
        
        # Add insight and recommendations
        result += "\nInsight & Rekomendasi:\n"
        
        # Find dominant brands
        brand_counts = {}
        for car in all_cars:
            brand_name = car.brand.name
            if brand_name in brand_counts:
                brand_counts[brand_name] += 1
            else:
                brand_counts[brand_name] = 1
        
        top_brands = sorted(brand_counts.items(), key=lambda x: x[1], reverse=True)[:3]
        
        if top_brands:
            result += f"1. Fokus pada pengembangan layanan khusus untuk merek {', '.join([b[0] for b in top_brands])}\n"
        
        # Analyze aging vehicles
        current_year = fields.Date.today().year
        old_cars = [car for car in all_cars if int(car.year) < current_year - 5]
        if old_cars:
            result += f"2. Potensi layanan perawatan untuk {len(old_cars)} kendaraan berusia >5 tahun\n"
        
        # Engine type insights
        if 'electric' in engine_types and engine_types['electric'] > 0:
            result += "3. Pertimbangkan pengembangan layanan khusus kendaraan listrik\n"
        
        return result

    def _get_customer_acquisition_analysis(self, date_from, date_to, message):
        """Analyze customer acquisition sources"""
        # Get customers in date range
        new_customers = self.env['res.partner'].search([
            ('create_date', '>=', date_from),
            ('create_date', '<=', date_to),
            ('customer_rank', '>', 0),
        ])
        
        if not new_customers:
            return f"Tidak ditemukan data akuisisi pelanggan baru dalam periode {date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}."
        
        result = f"Analisis Akuisisi Pelanggan ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n\n"
        
        # Total new customers
        result += f"Total Pelanggan Baru: {len(new_customers)}\n\n"
        
        # Acquisition by source
        sources = self.env['res.partner.source'].search([])
        
        result += "Akuisisi Berdasarkan Sumber:\n"
        for source in sources:
            customers = new_customers.filtered(lambda c: c.source.id == source.id)
            if customers:
                result += f"- {source.name}: {len(customers)} pelanggan ({len(customers)/len(new_customers)*100:.2f}%)\n"
        
        # Customers without source
        no_source = new_customers.filtered(lambda c: not c.source)
        if no_source:
            result += f"- Tidak Terdefinisi: {len(no_source)} pelanggan ({len(no_source)/len(new_customers)*100:.2f}%)\n"
        
        # Monthly trends
        months = {}
        for customer in new_customers:
            month = customer.create_date.strftime('%Y-%m')
            if month in months:
                months[month] += 1
            else:
                months[month] = 1
        
        result += "\nTren Akuisisi Bulanan:\n"
        for month, count in sorted(months.items()):
            month_name = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
            result += f"- {month_name}: {count} pelanggan baru\n"
        
        # Conversion analysis (registration to first purchase)
        conversion_times = []
        
        for customer in new_customers:
            first_order = self.env['sale.order'].search([
                ('partner_id', '=', customer.id),
                ('state', 'in', ['sale', 'done']),
            ], order='date_order asc', limit=1)
            
            if first_order:
                # Calculate days between registration and first purchase
                registration_date = customer.create_date.date()
                purchase_date = first_order.date_order.date()
                days_to_convert = (purchase_date - registration_date).days
                
                conversion_times.append(days_to_convert)
        
        if conversion_times:
            avg_conversion_time = sum(conversion_times) / len(conversion_times)
            converted_count = len(conversion_times)
            conversion_rate = converted_count / len(new_customers) * 100
            
            result += f"\nMetrik Konversi:\n"
            result += f"- Jumlah Pelanggan Baru yang Melakukan Pembelian: {converted_count} dari {len(new_customers)} ({conversion_rate:.2f}%)\n"
            result += f"- Rata-rata Waktu Konversi: {avg_conversion_time:.1f} hari\n"
            
            # Conversion by source
            result += "\nKonversi Berdasarkan Sumber:\n"
            for source in sources:
                source_customers = new_customers.filtered(lambda c: c.source.id == source.id)
                if not source_customers:
                    continue
                    
                source_converted = 0
                source_conversion_times = []
                
                for customer in source_customers:
                    first_order = self.env['sale.order'].search([
                        ('partner_id', '=', customer.id),
                        ('state', 'in', ['sale', 'done']),
                    ], order='date_order asc', limit=1)
                    
                    if first_order:
                        source_converted += 1
                        registration_date = customer.create_date.date()
                        purchase_date = first_order.date_order.date()
                        days_to_convert = (purchase_date - registration_date).days
                        source_conversion_times.append(days_to_convert)
                
                if source_customers:
                    source_conversion_rate = source_converted / len(source_customers) * 100
                    avg_source_conversion_time = sum(source_conversion_times) / len(source_conversion_times) if source_conversion_times else 0
                    
                    result += f"- {source.name}: {source_conversion_rate:.2f}% (avg. {avg_source_conversion_time:.1f} hari)\n"
        
        # Insights and recommendations
        result += "\nInsight & Rekomendasi:\n"
        
        # Identify best acquisition sources
        if sources:
            source_performance = {}
            for source in sources:
                source_customers = new_customers.filtered(lambda c: c.source.id == source.id)
                if not source_customers:
                    continue
                    
                source_converted = 0
                for customer in source_customers:
                    first_order = self.env['sale.order'].search([
                        ('partner_id', '=', customer.id),
                        ('state', 'in', ['sale', 'done']),
                    ], order='date_order asc', limit=1)
                    
                    if first_order:
                        source_converted += 1
                
                conversion_rate = source_converted / len(source_customers) * 100 if source_customers else 0
                source_performance[source.name] = {
                    'count': len(source_customers),
                    'conversion_rate': conversion_rate
                }
            
            # Best sources by volume
            volume_leaders = sorted(source_performance.items(), key=lambda x: x[1]['count'], reverse=True)[:2]
            if volume_leaders:
                result += f"1. Prioritaskan sumber akuisisi volume tertinggi: {', '.join([s[0] for s in volume_leaders])}\n"
            
            # Best sources by conversion
            conversion_leaders = sorted(source_performance.items(), key=lambda x: x[1]['conversion_rate'], reverse=True)[:2]
            if conversion_leaders:
                result += f"2. Optimalkan konversi dari sumber berkualitas: {', '.join([s[0] for s in conversion_leaders])}\n"
        
        # Highlight sources that need improvement
        low_performers = []
        for source_name, data in source_performance.items():
            if data['count'] >= 5 and data['conversion_rate'] < 10:  # Minimal 5 pelanggan, konversi < 10%
                low_performers.append(source_name)
        
        if low_performers:
            result += f"3. Evaluasi dan tingkatkan kualitas akuisisi dari: {', '.join(low_performers)}\n"
        
        # General recommendations
        result += "4. Implementasikan program onboarding untuk mempercepat konversi pelanggan baru\n"
        
        # If many customers without source
        if no_source and len(no_source) / len(new_customers) > 0.2:  # > 20% tidak memiliki source
            result += "5. Perbaiki proses pencatatan sumber akuisisi pelanggan untuk analisis yang lebih akurat\n"
        
        return result

    def _get_customer_retention_analysis(self, date_from, date_to, message):
        """Analyze customer retention and loyalty"""
        # Define time periods
        current_period_start = date_from
        current_period_end = date_to
        
        # Calculate previous period of same length
        period_length = (date_to - date_from).days
        previous_period_end = date_from - timedelta(days=1)
        previous_period_start = previous_period_end - timedelta(days=period_length)
        
        # Get active customers in current period
        current_orders = self.env['sale.order'].search([
            ('date_order', '>=', current_period_start),
            ('date_order', '<=', current_period_end),
            ('state', 'in', ['sale', 'done']),
        ])
        
        current_customers = self.env['res.partner'].browse(current_orders.mapped('partner_id.id'))
        
        # Get active customers in previous period
        previous_orders = self.env['sale.order'].search([
            ('date_order', '>=', previous_period_start),
            ('date_order', '<=', previous_period_end),
            ('state', 'in', ['sale', 'done']),
        ])
        
        previous_customers = self.env['res.partner'].browse(previous_orders.mapped('partner_id.id'))
        
        # Calculate retention metrics
        retained_customers = current_customers.filtered(lambda c: c.id in previous_customers.ids)
        new_customers = current_customers.filtered(lambda c: c.id not in previous_customers.ids)
        churned_customers = previous_customers.filtered(lambda c: c.id not in current_customers.ids)
        
        retention_rate = len(retained_customers) / len(previous_customers) * 100 if previous_customers else 0
        churn_rate = len(churned_customers) / len(previous_customers) * 100 if previous_customers else 0
        
        result = f"Analisis Retensi Pelanggan ({date_from.strftime('%Y-%m-%d')} hingga {date_to.strftime('%Y-%m-%d')}):\n\n"
        
        # Basic metrics
        result += "Metrik Retensi:\n"
        result += f"- Pelanggan Aktif Periode Ini: {len(current_customers)}\n"
        result += f"- Pelanggan Aktif Periode Sebelumnya: {len(previous_customers)}\n"
        result += f"- Pelanggan yang Dipertahankan: {len(retained_customers)}\n"
        result += f"- Pelanggan Baru: {len(new_customers)}\n"
        result += f"- Pelanggan Churn: {len(churned_customers)}\n"
        result += f"- Tingkat Retensi: {retention_rate:.2f}%\n"
        result += f"- Tingkat Churn: {churn_rate:.2f}%\n"
        
        # Repeat purchase analysis
        repeat_customers = {}
        for customer_id in current_customers.ids:
            customer_orders = current_orders.filtered(lambda o: o.partner_id.id == customer_id)
            order_count = len(customer_orders)
            repeat_customers[customer_id] = order_count
        
        # Group by frequency
        frequency_segments = {
            '1 order': 0,
            '2 orders': 0,
            '3 orders': 0,
            '4-5 orders': 0,
            '6+ orders': 0
        }
        
        for customer_id, order_count in repeat_customers.items():
            if order_count == 1:
                frequency_segments['1 order'] += 1
            elif order_count == 2:
                frequency_segments['2 orders'] += 1
            elif order_count == 3:
                frequency_segments['3 orders'] += 1
            elif 4 <= order_count <= 5:
                frequency_segments['4-5 orders'] += 1
            else:
                frequency_segments['6+ orders'] += 1
        
        result += "\nFrekuensi Pembelian (Periode Ini):\n"
        for label, count in frequency_segments.items():
            result += f"- {label}: {count} pelanggan ({count/len(current_customers)*100:.2f}%)\n"
        
        # Loyalty analysis (customers with repeat purchases over time)
        all_time_orders = self.env['sale.order'].search([
            ('state', 'in', ['sale', 'done']),
        ])
        
        customer_order_history = {}
        for order in all_time_orders:
            customer_id = order.partner_id.id
            if customer_id in customer_order_history:
                customer_order_history[customer_id].append(order.date_order)
            else:
                customer_order_history[customer_id] = [order.date_order]
        
        # Calculate customer lifetime (in days) and frequency
        loyalty_metrics = {}
        for customer_id, order_dates in customer_order_history.items():
            if len(order_dates) <= 1:
                continue
                
            sorted_dates = sorted(order_dates)
            first_order = sorted_dates[0]
            last_order = sorted_dates[-1]
            lifetime_days = (last_order - first_order).days
            
            loyalty_metrics[customer_id] = {
                'lifetime_days': lifetime_days,
                'order_count': len(order_dates),
                'frequency': lifetime_days / (len(order_dates) - 1) if len(order_dates) > 1 and lifetime_days > 0 else 0
            }
        
        # Calculate averages
        if loyalty_metrics:
            avg_lifetime = sum(m['lifetime_days'] for m in loyalty_metrics.values()) / len(loyalty_metrics)
            avg_order_count = sum(m['order_count'] for m in loyalty_metrics.values()) / len(loyalty_metrics)
            avg_purchase_frequency = [m['frequency'] for m in loyalty_metrics.values() if m['frequency'] > 0]
            avg_purchase_frequency = sum(avg_purchase_frequency) / len(avg_purchase_frequency) if avg_purchase_frequency else 0
            
            result += "\nMetrik Loyalitas Pelanggan:\n"
            result += f"- Rata-rata Durasi Hubungan: {avg_lifetime:.1f} hari\n"
            result += f"- Rata-rata Jumlah Order per Pelanggan: {avg_order_count:.2f}\n"
            result += f"- Rata-rata Frekuensi Pembelian: {avg_purchase_frequency:.1f} hari\n"
        
        # Analysis of churned customers
        # Analysis of churned customers
        if churned_customers:
            result += "\nAnalisis Pelanggan Churn:\n"
            
            # Last order date analysis
            days_since_last_order = []
            for customer in churned_customers:
                last_order = self.env['sale.order'].search([
                    ('partner_id', '=', customer.id),
                    ('state', 'in', ['sale', 'done']),
                ], order='date_order desc', limit=1)
                
                if last_order:
                    days_since = (fields.Date.today() - last_order.date_order.date()).days
                    days_since_last_order.append(days_since)
            
            if days_since_last_order:
                avg_days_since_last_order = sum(days_since_last_order) / len(days_since_last_order)
                result += f"- Rata-rata hari sejak order terakhir: {avg_days_since_last_order:.1f} hari\n"
            
            # Check if churned customers have returned in current period
            returned_customers = 0
            for customer in churned_customers:
                orders_in_current = current_orders.filtered(lambda o: o.partner_id.id == customer.id)
                if orders_in_current:
                    returned_customers += 1
            
            recovery_rate = returned_customers / len(churned_customers) * 100 if churned_customers else 0
            result += f"- Pelanggan churn yang kembali: {returned_customers} ({recovery_rate:.2f}%)\n"
        
        # Insights and recommendations
        result += "\nInsight & Rekomendasi:\n"
        
        # Retention insights
        if retention_rate < 30:
            result += "1. Tingkat retensi sangat rendah, prioritaskan program retensi pelanggan\n"
        elif retention_rate < 50:
            result += "1. Tingkatkan program retensi untuk mencapai target minimum 50%\n"
        else:
            result += "1. Pertahankan strategi retensi yang efektif saat ini\n"
        
        # Churn insights
        if churned_customers:
            result += "2. Implementasikan program win-back untuk pelanggan churn\n"
            
            # If churned customers tend to have long gaps between purchases
            if 'avg_days_since_last_order' in locals() and avg_days_since_last_order > 90:
                result += f"3. Buat program reengagement untuk pelanggan yang tidak bertransaksi >90 hari\n"
        
        # Loyalty insights
        high_frequency_ratio = (frequency_segments['4-5 orders'] + frequency_segments['6+ orders']) / len(current_customers) if current_customers else 0
        
        if high_frequency_ratio < 0.1:  # Less than 10% are high-frequency customers
            result += "4. Tingkatkan frekuensi pembelian dengan program insentif dan komunikasi reguler\n"
        
        # General recommendations
        result += "5. Implementasikan program loyalitas berbasis tier untuk mendorong repeat purchase\n"
        result += "6. Analisis pola pembelian untuk identifikasi peluang cross-selling dan up-selling\n"
        
        return result
    
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
                    context_data = self._get_employees_data(content)  # Replace with existing method
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
        
    def _update_chat_name_from_first_message(self, content):
        """Memperbarui nama chat berdasarkan pesan pertama"""
        try:
            # Ambil 30 karakter pertama dari pesan sebagai nama awal
            new_name = content[:30].strip()
            if new_name:
                self.write({'name': new_name})
        except Exception as e:
            _logger.error(f"Error saat memperbarui nama chat: {str(e)}")
        
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
            # Kata kunci untuk karyawan dan kinerja
            'attendance', 'hadir', 'absensi', 'kehadiran', 'performance', 'kinerja',
            'service advisor', 'mechanic', 'mekanik', 'lead time', 'durasi', 'rating',
            # Kata kunci untuk produk, booking, dan rekomendasi
            'product', 'item', 'produk', 'barang', 'part', 'sparepart',
            'booking', 'reservasi', 'janji', 'appointment', 'jadwal',
            'recommendation', 'rekomendasi', 'saran', 'suggest',
            # Kata kunci untuk prediksi
            'predict', 'forecast', 'projection', 'prediksi', 'proyeksi', 'perkiraan', 'future', 'masa depan',
            # Kata kunci untuk perilaku pelanggan
            'customer behavior', 'perilaku pelanggan', 'segmentation', 'segmentasi', 'loyal', 'loyalty', 'retention', 'retensi',
            # Kata kunci untuk peluang bisnis
            'opportunity', 'peluang', 'potensi', 'potential', 'growth', 'pertumbuhan',
            # Kata kunci untuk efisiensi workflow
            'workflow', 'efficiency', 'process', 'bottleneck', 'efisiensi', 'proses',
            # Kata kunci untuk analisis RFM
            'rfm', 'recency', 'frequency', 'monetary', 'segmentasi pelanggan'
        ]
        
        message_lower = message.lower()
        
        # Cek jika ada kata kunci bisnis
        for keyword in business_keywords:
            if keyword in message_lower:
                return True
        
        # Jika tidak ada kata kunci bisnis, cek jika pertanyaan tentang data perusahaan
        data_indicators = ['how many', 'how much', 'berapa', 'total', 'average', 'rata-rata', 
                        'performance', 'kinerja', 'compare', 'bandingkan', 'analyze', 
                        'analisis', 'report', 'laporan', 'trend', 'tren', 'growth', 'pertumbuhan']
        
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
        """Analyze message and gather relevant data from Odoo, including enhanced data and predictive analytics"""
        message_lower = message.lower()
        
        # Cek apakah pertanyaan meminta laporan komprehensif
        if any(k in message_lower for k in ['komprehensif', 'lengkap', 'menyeluruh', 'comprehensive', 'report', 'laporan']):
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
        if any(k in message_lower for k in ['karyawan', 'employee', 'absensi', 'attendance', 'hadir', 'pegawai', 'staff', 'hr', 'sdm']):
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
        if any(k in message_lower for k in ['lead time', 'durasi', 'waktu', 'efficiency', 'efisiensi', 'process', 'proses']):
            lead_time_data = self._get_lead_time_analysis(None, None, message)
            if lead_time_data:
                result.append(lead_time_data)
        
        # Cek untuk data produk
        if any(k in message_lower for k in ['product', 'produk', 'barang', 'part', 'sparepart', 'item', 'katalog']):
            product_data = self._get_product_data(message)
            if product_data:
                result.append(product_data)
        
        # Cek untuk data booking servis
        if any(k in message_lower for k in ['booking', 'reservasi', 'janji', 'appointment', 'jadwal', 'schedule', 'calendar']):
            booking_data = self._get_booking_data(message)
            if booking_data:
                result.append(booking_data)
        
        # Cek untuk data rekomendasi servis
        if any(k in message_lower for k in ['recommendation', 'rekomendasi', 'saran', 'suggest', 'usulan', 'advice']):
            recommendation_data = self._get_service_recommendation_data(message)
            if recommendation_data:
                result.append(recommendation_data)
        
        # Cek untuk prediksi penjualan
        if any(k in message_lower for k in ['predict', 'forecast', 'projection', 'prediksi', 'proyeksi', 'perkiraan', 'future', 'masa depan', 'trend', 'tren']):
            prediction_data = self._get_sales_prediction(message)
            if prediction_data:
                result.append(prediction_data)
        
        # Cek untuk analisis perilaku pelanggan
        if any(k in message_lower for k in ['customer behavior', 'perilaku pelanggan', 'segmentation', 'segmentasi', 'loyal', 'loyalty', 'retention', 'retensi', 'churn']):
            behavior_data = self._get_customer_behavior_analysis(message)
            if behavior_data:
                result.append(behavior_data)
        
        # Cek untuk analisis peluang bisnis
        if any(k in message_lower for k in ['opportunity', 'peluang', 'potensi', 'potential', 'growth', 'pertumbuhan', 'market', 'pasar', 'strategic']):
            opportunity_data = self._get_business_opportunity_analysis(message)
            if opportunity_data:
                result.append(opportunity_data)
        
        # Cek untuk analisis efisiensi workflow
        if any(k in message_lower for k in ['workflow', 'efficiency', 'process', 'bottleneck', 'efisiensi', 'proses', 'optimization', 'optimasi']):
            workflow_data = self._get_workflow_efficiency_analysis(message)
            if workflow_data:
                result.append(workflow_data)
        
        # Cek untuk analisis RFM
        if any(k in message_lower for k in ['rfm', 'recency', 'frequency', 'monetary', 'segmentasi pelanggan', 'customer value']):
            rfm_data = self._get_rfm_analysis(message)
            if rfm_data:
                result.append(rfm_data)

        # Tambahkan pengecekan untuk analisis customer
        if any(k in message_lower for keyword_list in [
            ['customer', 'pelanggan', 'client', 'klien', 'konsumen'], 
            ['retention', 'retensi', 'loyal', 'loyalty', 'churn'],
            ['acquisition', 'akuisisi', 'new customer', 'pelanggan baru'],
            ['segment', 'segmentasi', 'profile', 'profil', 'demographic', 'demografi']
        ] for k in keyword_list):
            customer_data = self._get_customer_data(message)
            if customer_data:
                result.append(customer_data)
        
        # Join semua data dengan separator
        if result:
            return "\n\n---\n\n".join(result)
        else:
            # Default basic company data if no specific data found
            return self._get_basic_company_data()
    
    def _analyze_message(self, message):
        """Analyze the message to determine what data to fetch"""
        message_lower = message.lower()
        
        # Define categories with keywords - enhanced with more keywords
        categories = {
            'sales': ['sales', 'revenue', 'customer', 'order', 'client', 'income', 'penjualan', 'pelanggan', 'pendapatan', 'pesanan', 'omzet', 'omset', 'transaction', 'transaksi', 'volume', 'growth', 'pertumbuhan', 'performance', 'performa', 'conversion', 'konversi'],
            'inventory': ['inventory', 'stock', 'product', 'warehouse', 'item', 'persediaan', 'stok', 'produk', 'gudang', 'barang', 'sparepart', 'part', 'supply', 'pasokan', 'goods', 'material', 'katalog', 'catalog'],            
            'finance': ['invoice', 'payment', 'profit', 'loss', 'accounting', 'balance', 'faktur', 'pembayaran', 'keuntungan', 'kerugian', 'akuntansi', 'saldo', 'cash flow', 'arus kas', 'revenue', 'pendapatan', 'expense', 'biaya', 'margin', 'tax', 'pajak', 'debt', 'utang', 'receivable', 'piutang'],           
            'employees': ['employee', 'staff', 'hr', 'attendance', 'absensi', 'karyawan', 'pegawai', 'sdm', 'hadir', 'kehadiran', 'performance', 'kinerja', 'productivity', 'produktivitas', 'skill', 'kompensasi', 'compensation', 'payroll', 'penggajian', 'training', 'pelatihan'],            
            'purchases': ['purchase', 'vendor', 'supplier', 'pembelian', 'vendor', 'pemasok', 'procurement', 'pengadaan', 'order', 'pesanan', 'requisition', 'permintaan', 'delivery', 'pengiriman', 'po', 'purchase order'],            
            'service': ['service', 'advisor', 'mechanic', 'mekanik', 'servis', 'lead time', 'durasi', 'sa', 'sparepart', 'part', 'customer satisfaction', 'kepuasan pelanggan', 'quality', 'kualitas', 'maintenance', 'perawatan', 'repair', 'perbaikan', 'workshop', 'bengkel', 'stall', 'pit'],           
            'product': ['product', 'item', 'produk', 'barang', 'part', 'sparepart', 'harga', 'stok', 'price', 'kategori', 'category', 'brand', 'merek', 'availability', 'ketersediaan', 'specification', 'spesifikasi', 'feature', 'fitur'],            
            'booking': ['booking', 'reservasi', 'janji', 'appointment', 'jadwal', 'schedule', 'calendar', 'kalender', 'slot', 'availability', 'ketersediaan', 'service booking', 'booking servis', 'queue', 'antrian'],
            'recommendation': ['recommendation', 'rekomendasi', 'saran', 'suggest', 'usulan', 'advice', 'proposal', 'suggest', 'cross-sell', 'up-sell', 'bundling'],            
            'prediction': ['predict', 'forecast', 'projection', 'prediksi', 'proyeksi', 'perkiraan', 'future', 'masa depan', 'trend', 'tren', 'growth', 'pertumbuhan', 'estimation', 'estimasi', 'model', 'pattern', 'pola'],
            'customer_behavior': ['customer behavior', 'perilaku pelanggan', 'segmentation', 'segmentasi', 'loyal', 'loyalty', 'retention', 'retensi', 'churn', 'preference', 'preferensi', 'habit', 'kebiasaan', 'persona', 'profile', 'profil', 'behavior', 'perilaku'],
            'business_opportunity': ['opportunity', 'peluang', 'potensi', 'potential', 'growth', 'pertumbuhan', 'expansion', 'ekspansi', 'investment', 'investasi', 'strategic', 'strategis', 'development', 'pengembangan', 'diversification', 'diversifikasi', 'market', 'pasar'],
            'workflow_efficiency': ['workflow', 'efficiency', 'process', 'bottleneck', 'efisiensi', 'proses', 'optimization', 'optimasi', 'improvement', 'perbaikan', 'productivity', 'produktivitas', 'throughput', 'lead time', 'waiting time', 'waktu tunggu', 'cycle time', 'siklus'],
            'rfm_analysis': ['rfm', 'recency', 'frequency', 'monetary', 'segmentasi pelanggan', 'customer segment', 'segment value', 'nilai segmen', 'purchase pattern', 'pola pembelian', 'customer value', 'nilai pelanggan', 'top customer', 'pelanggan utama'],
            'customer': ['customer', 'pelanggan', 'client', 'klien', 'konsumen', 'repeat purchase', 'pembelian berulang', 'customer analysis', 
                    'analisis pelanggan', 'demografi', 'demographic', 'gender', 'jenis kelamin',
                    'customer behavior', 'perilaku pelanggan', 'profile', 'profil', 'retention', 'retensi',
                    'loyalty', 'loyalitas', 'churn', 'acquisition', 'akuisisi', 'new customer', 'pelanggan baru']
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
        """Get finance data based on the user's message with improved historical comparison"""
        # Determine time period from message
        time_period = self._extract_time_period(message)
        
        # Get date range for the period
        date_from, date_to = self._get_date_range(time_period)
        
        # Menentukan periode perbandingan (bulan lalu)
        if time_period == 'this_month':
            today = fields.Date.today()
            current_month_start = today.replace(day=1)
            
            # Menentukan tanggal pertama bulan lalu
            if current_month_start.month == 1:
                prev_month_start = current_month_start.replace(year=current_month_start.year-1, month=12)
            else:
                prev_month_start = current_month_start.replace(month=current_month_start.month-1)
            
            # Menentukan tanggal terakhir bulan lalu
            prev_month_end = current_month_start - timedelta(days=1)
            
            # Periode komparasi
            comparison_date_from = prev_month_start
            comparison_date_to = prev_month_end
        elif time_period == 'this_week':
            # Logika untuk perbandingan minggu ini vs minggu lalu
            today = fields.Date.today()
            start_of_week = today - timedelta(days=today.weekday())
            
            # Minggu lalu
            comparison_date_from = start_of_week - timedelta(days=7)
            comparison_date_to = comparison_date_from + timedelta(days=6)
        else:
            # Default perbandingan: periode saat ini vs periode sebelumnya
            period_length = (date_to - date_from).days
            comparison_date_to = date_from - timedelta(days=1)
            comparison_date_from = comparison_date_to - timedelta(days=period_length)
        
        # Query invoices for current period
        invoice_domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        invoices = self.env['account.move'].search(invoice_domain)
        
        # Query invoices for comparison period
        comparison_invoice_domain = [
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', comparison_date_from),
            ('invoice_date', '<=', comparison_date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        comparison_invoices = self.env['account.move'].search(comparison_invoice_domain)
        
        # Query expenses (bills) for current period
        bill_domain = [
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '>=', date_from),
            ('invoice_date', '<=', date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        bills = self.env['account.move'].search(bill_domain)
        
        # Query expenses (bills) for comparison period
        comparison_bill_domain = [
            ('move_type', '=', 'in_invoice'),
            ('invoice_date', '>=', comparison_date_from),
            ('invoice_date', '<=', comparison_date_to),
            ('state', '=', 'posted'),
            ('company_id', '=', self.company_id.id)
        ]
        
        comparison_bills = self.env['account.move'].search(comparison_bill_domain)
        
        # Calculate metrics for current period
        total_invoices = len(invoices)
        total_invoice_amount = sum(invoices.mapped('amount_total'))
        
        total_bills = len(bills)
        total_bill_amount = sum(bills.mapped('amount_total'))
        
        # Calculate metrics for comparison period
        comparison_total_invoices = len(comparison_invoices)
        comparison_total_invoice_amount = sum(comparison_invoices.mapped('amount_total'))
        
        comparison_total_bills = len(comparison_bills)
        comparison_total_bill_amount = sum(comparison_bills.mapped('amount_total'))
        
        # Calculate profit margins
        profit = total_invoice_amount - total_bill_amount
        profit_margin = (profit / total_invoice_amount * 100) if total_invoice_amount else 0
        
        comparison_profit = comparison_total_invoice_amount - comparison_total_bill_amount
        comparison_profit_margin = (comparison_profit / comparison_total_invoice_amount * 100) if comparison_total_invoice_amount else 0
        
        # Calculate percent changes
        if comparison_total_invoice_amount:
            revenue_change_pct = ((total_invoice_amount - comparison_total_invoice_amount) / comparison_total_invoice_amount) * 100
        else:
            revenue_change_pct = None
            
        if comparison_total_bill_amount:
            expense_change_pct = ((total_bill_amount - comparison_total_bill_amount) / comparison_total_bill_amount) * 100
        else:
            expense_change_pct = None
            
        if comparison_profit:
            profit_change_pct = ((profit - comparison_profit) / comparison_profit) * 100
        else:
            profit_change_pct = None
        
        # Format data
        result = f"""
Ringkasan Keuangan ({date_from.strftime('%d %B %Y')} hingga {date_to.strftime('%d %B %Y')}):

Pendapatan:
- Periode Ini: {total_invoice_amount:,.2f}
- Periode Sebelumnya: {comparison_total_invoice_amount:,.2f}
"""

        if revenue_change_pct is not None:
            direction = "naik" if revenue_change_pct > 0 else "turun"
            result += f"- Perubahan: {direction} {abs(revenue_change_pct):.2f}%\n"
        else:
            result += "- Perubahan: Tidak ada data pembanding\n"

        result += f"""
Pengeluaran:
- Periode Ini: {total_bill_amount:,.2f}
- Periode Sebelumnya: {comparison_total_bill_amount:,.2f}
"""

        if expense_change_pct is not None:
            direction = "naik" if expense_change_pct > 0 else "turun"
            result += f"- Perubahan: {direction} {abs(expense_change_pct):.2f}%\n"
        else:
            result += "- Perubahan: Tidak ada data pembanding\n"

        result += f"""
Keuntungan:
- Periode Ini: {profit:,.2f}
- Margin Periode Ini: {profit_margin:.2f}%
- Periode Sebelumnya: {comparison_profit:,.2f}
- Margin Periode Sebelumnya: {comparison_profit_margin:.2f}%
"""

        if profit_change_pct is not None:
            direction = "naik" if profit_change_pct > 0 else "turun"
            result += f"- Perubahan Keuntungan: {direction} {abs(profit_change_pct):.2f}%\n"
        else:
            result += "- Perubahan Keuntungan: Tidak ada data pembanding\n"
        
        # Tambahkan analisis dan rekomendasi
        result += "\nAnalisis Kinerja Keuangan:\n"
        
        # Analisis pendapatan
        if revenue_change_pct is not None:
            if revenue_change_pct > 5:
                result += "- Pendapatan mengalami peningkatan signifikan, menandakan pertumbuhan bisnis yang positif.\n"
            elif revenue_change_pct > 0:
                result += "- Pendapatan menunjukkan pertumbuhan moderat dibandingkan periode sebelumnya.\n"
            elif revenue_change_pct > -5:
                result += "- Pendapatan relatif stabil dengan sedikit penurunan dibandingkan periode sebelumnya.\n"
            else:
                result += "- Pendapatan mengalami penurunan signifikan, perlu evaluasi faktor penyebabnya.\n"
        
        # Analisis pengeluaran
        if expense_change_pct is not None:
            if expense_change_pct > 10:
                result += "- Pengeluaran meningkat signifikan, perlu dilakukan evaluasi efisiensi biaya.\n"
            elif expense_change_pct > revenue_change_pct and expense_change_pct > 0:
                result += "- Pengeluaran tumbuh lebih cepat dari pendapatan, perhatikan efisiensi operasional.\n"
            elif expense_change_pct < 0:
                result += "- Pengeluaran berhasil ditekan, menunjukkan efisiensi operasional yang baik.\n"
        
        # Analisis margin
        if comparison_profit_margin:
            if profit_margin > comparison_profit_margin:
                result += "- Margin keuntungan meningkat, menunjukkan efisiensi bisnis yang lebih baik.\n"
            else:
                result += "- Margin keuntungan menurun, perlu ditingkatkan pengelolaan pendapatan dan biaya.\n"
        
        # Rekomendasi
        result += "\nRekomendasi:\n"
        if profit_margin < 15:
            result += "- Tingkatkan margin keuntungan dengan optimalisasi harga atau efisiensi operasional.\n"
        
        if expense_change_pct is not None and expense_change_pct > 0:
            result += "- Lakukan audit pengeluaran untuk mengidentifikasi area penghematan potensial.\n"
        
        if revenue_change_pct is not None and revenue_change_pct < 0:
            result += "- Tingkatkan strategi pemasaran dan penjualan untuk mendorong pertumbuhan pendapatan.\n"
        
        result += "- Pantau rasio keuangan secara reguler untuk identifikasi tren dan pengambilan keputusan proaktif.\n"
        
        return result

    def _get_finance_data_from_journals(self, date_from, date_to, comparison_date_from, comparison_date_to):
        """Get financial data from journal entries for more comprehensive analysis"""
        
        # Query untuk periode saat ini
        current_period_query = """
            SELECT
                CASE 
                    WHEN account.internal_type = 'receivable' OR account.internal_type = 'other' AND account.internal_group = 'income' THEN 'income'
                    WHEN account.internal_type = 'payable' OR account.internal_type = 'other' AND account.internal_group = 'expense' THEN 'expense'
                    ELSE account.internal_group
                END as account_type,
                SUM(line.balance) as total
            FROM
                account_move_line line
            JOIN
                account_move move ON line.move_id = move.id
            JOIN
                account_account account ON line.account_id = account.id
            WHERE
                move.state = 'posted'
                AND move.company_id = %s
                AND move.date >= %s
                AND move.date <= %s
            GROUP BY
                account_type
        """
        
        # Execute query untuk periode saat ini
        self.env.cr.execute(current_period_query, (self.company_id.id, date_from, date_to))
        current_results = self.env.cr.dictfetchall()
        
        # Initialize values
        current_income = 0
        current_expense = 0
        
        # Process results
        for row in current_results:
            if row['account_type'] == 'income':
                current_income += abs(row['total'])
            elif row['account_type'] == 'expense':
                current_expense += abs(row['total'])
        
        # Repeat for comparison period
        self.env.cr.execute(current_period_query, (self.company_id.id, comparison_date_from, comparison_date_to))
        comparison_results = self.env.cr.dictfetchall()
        
        comparison_income = 0
        comparison_expense = 0
        
        for row in comparison_results:
            if row['account_type'] == 'income':
                comparison_income += abs(row['total'])
            elif row['account_type'] == 'expense':
                comparison_expense += abs(row['total'])
        
        return {
            'current_income': current_income,
            'current_expense': current_expense,
            'comparison_income': comparison_income,
            'comparison_expense': comparison_expense
        }
    
    def _cache_financial_data(self):
        """Cache financial data for previous periods to improve performance"""
        today = fields.Date.today()
        current_month_start = today.replace(day=1)
        
        # Tentukan bulan lalu
        if current_month_start.month == 1:
            prev_month_start = current_month_start.replace(year=current_month_start.year-1, month=12)
        else:
            prev_month_start = current_month_start.replace(month=current_month_start.month-1)
        
        prev_month_end = current_month_start - timedelta(days=1)
        
        # Query data dan simpan ke cache
        financial_data = self._get_finance_data_from_journals(
            prev_month_start, prev_month_end, 
            None, None  # Tidak perlu perbandingan
        )
        
        # Simpan ke parameter sistem
        self.env['ir.config_parameter'].sudo().set_param(
            f'finance_cache_prev_month_{self.company_id.id}',
            json.dumps({
                'date_from': prev_month_start.isoformat(),
                'date_to': prev_month_end.isoformat(),
                'income': financial_data['current_income'],
                'expense': financial_data['current_expense']
            })
        )
        
        _logger.info(f"Financial data cached for {prev_month_start} to {prev_month_end}")
        return True
    
    # Tambahkan di __init__ atau setup
    def _setup_ai_finance_access(self):
        """Set up proper access for AI to financial data"""
        # Create parameter jika belum ada
        if not self.env['ir.config_parameter'].sudo().get_param('ai_finance_months_to_include'):
            self.env['ir.config_parameter'].sudo().set_param('ai_finance_months_to_include', '6')
        
        # Caching bulan lalu jika belum ada
        self._cache_financial_data()
        
        return True
    
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
                
                # TAMBAHKAN KODE VALIDASI DI SINI
                # Validasi tanggal masa depan
                current_date = fields.Date.today()
                if datetime(year, month, 1).date() > current_date:
                    # Gunakan bulan saat ini jika tanggal di masa depan
                    year_now = current_date.year
                    month_now = current_date.month
                    start_date = datetime(year_now, month_now, 1).date()
                    if month_now == 12:
                        end_month = datetime(year_now+1, 1, 1).date() - timedelta(days=1)
                    else:
                        end_month = datetime(year_now, month_now+1, 1).date() - timedelta(days=1)
                    return start_date, end_month
                
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
