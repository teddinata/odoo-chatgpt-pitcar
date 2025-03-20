from odoo import http
from odoo.http import request, content_disposition
import json
import logging
from werkzeug.exceptions import BadRequest, Unauthorized, NotFound
from datetime import datetime

_logger = logging.getLogger(__name__)

class AIController(http.Controller):
    
    @http.route('/web/ai/chat/session', type='http', auth='user', methods=['POST'], csrf=False)
    def create_chat_session(self, **post):
        """Create a new chat session"""
        try:
            # Get all active chats for the user
            chats = request.env['ai.chat'].sudo().search([
                ('user_id', '=', request.env.user.id),
                ('active', '=', True)
            ], order='last_message_date desc')
            
            result = []
            for chat in chats:
                # Get the last message
                last_message = chat.message_ids.sorted('create_date', reverse=True)[:1]
                
                result.append({
                    'id': chat.id,
                    'name': chat.name,
                    'created_at': chat.create_date.isoformat(),
                    'last_message_date': chat.last_message_date.isoformat() if chat.last_message_date else None,
                    'last_message': last_message.content[:100] + '...' if last_message else None,
                    'total_messages': len(chat.message_ids),
                    'total_tokens': chat.total_tokens,
                    'topic': chat.topic or 'New Chat',
                    'summary': chat.summary or None,
                })
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'chats': result
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error getting chat list: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/chat/<int:chat_id>/archive', type='http', auth='user', methods=['POST'], csrf=False)
    def archive_chat(self, chat_id, **post):
        """Archive a chat"""
        try:
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': 'Chat not found or access denied'
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=404
                )
            
            # Archive the chat
            chat.archive_chat()
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'message': 'Chat archived successfully'
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error archiving chat: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/chat/<int:chat_id>/restore', type='http', auth='user', methods=['POST'], csrf=False)
    def restore_chat(self, chat_id, **post):
        """Restore an archived chat"""
        try:
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': 'Chat not found or access denied'
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=404
                )
            
            # Restore the chat
            chat.restore_chat()
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'message': 'Chat restored successfully'
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error restoring chat: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/chat/<int:chat_id>/clear', type='http', auth='user', methods=['POST'], csrf=False)
    def clear_chat(self, chat_id, **post):
        """Clear all messages in a chat"""
        try:
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': 'Chat not found or access denied'
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=404
                )
            
            # Clear the chat
            chat.clear_messages()
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'message': 'Chat cleared successfully'
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error clearing chat: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/settings', type='http', auth='user', methods=['GET'], csrf=False)
    def get_ai_settings(self, **params):
        """Get AI settings for the current user"""
        try:
            # Get user settings
            user_settings = request.env['ai.user.settings'].sudo().get_user_settings()
            
            # Get global settings
            api_key_set = bool(request.env['ir.config_parameter'].sudo().get_param('openai.api_key'))
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'settings': {
                        'default_model': user_settings.default_model,
                        'daily_gpt4_limit': user_settings.daily_gpt4_limit,
                        'gpt4_usage_count': user_settings.gpt4_usage_count,
                        'remaining_gpt4': max(0, user_settings.daily_gpt4_limit - user_settings.gpt4_usage_count),
                        'fallback_to_gpt35': user_settings.fallback_to_gpt35,
                        'token_usage_this_month': user_settings.token_usage_this_month,
                        'api_key_configured': api_key_set,
                        'temperature': user_settings.temperature,
                        'max_tokens': user_settings.max_tokens,
                        'has_custom_prompt': bool(user_settings.custom_system_prompt),
                    }
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error getting AI settings: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/settings', type='http', auth='user', methods=['POST'], csrf=False)
    def update_ai_settings(self, **post):
        """Update AI settings for the current user"""
        try:
            # Get user settings
            user_settings = request.env['ai.user.settings'].sudo().get_user_settings()
            
            # Get request data
            data = json.loads(request.httprequest.data.decode('utf-8'))
            
            # Update settings
            values = {}
            
            if 'default_model' in data:
                values['default_model'] = data['default_model']
                
            if 'temperature' in data and isinstance(data['temperature'], (int, float)):
                values['temperature'] = min(max(data['temperature'], 0.0), 2.0)  # Limit to range 0-2
                
            if 'max_tokens' in data and isinstance(data['max_tokens'], int):
                values['max_tokens'] = min(max(data['max_tokens'], 100), 4000)  # Limit to range 100-4000
                
            if 'custom_system_prompt' in data:
                values['custom_system_prompt'] = data['custom_system_prompt']
                
            if 'fallback_to_gpt35' in data:
                values['fallback_to_gpt35'] = bool(data['fallback_to_gpt35'])
            
            # Apply updates if any
            if values:
                user_settings.write(values)
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'message': 'Settings updated successfully',
                    'settings': {
                        'default_model': user_settings.default_model,
                        'daily_gpt4_limit': user_settings.daily_gpt4_limit,
                        'gpt4_usage_count': user_settings.gpt4_usage_count,
                        'remaining_gpt4': max(0, user_settings.daily_gpt4_limit - user_settings.gpt4_usage_count),
                        'fallback_to_gpt35': user_settings.fallback_to_gpt35,
                        'token_usage_this_month': user_settings.token_usage_this_month,
                        'temperature': user_settings.temperature,
                        'max_tokens': user_settings.max_tokens,
                        'has_custom_prompt': bool(user_settings.custom_system_prompt),
                    }
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error updating AI settings: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
            
    @http.route('/web/ai/export/<int:chat_id>', type='http', auth='user', methods=['GET'], csrf=False)
    def export_chat(self, chat_id, **params):
        """Export chat to various formats (json, txt, html)"""
        try:
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return request.not_found()
            
            # Get format
            export_format = params.get('format', 'json')
            if export_format not in ['json', 'txt', 'html', 'markdown']:
                export_format = 'json'
            
            # Get messages
            messages = chat.message_ids.sorted('create_date')
            
            if export_format == 'json':
                # Export as JSON
                result = {
                    'chat': {
                        'id': chat.id,
                        'name': chat.name,
                        'created_at': chat.create_date.isoformat(),
                        'last_message': chat.last_message_date.isoformat() if chat.last_message_date else None,
                    },
                    'messages': []
                }
                
                for message in messages:
                    result['messages'].append({
                        'id': message.id,
                        'message_id': message.message_uuid,
                        'content': message.content,
                        'type': message.message_type,
                        'model_used': message.model_used,
                        'timestamp': message.create_date.isoformat(),
                        'token_count': message.token_count or 0,
                    })
                
                # Generate the file
                data = json.dumps(result, indent=2)
                filename = f"chat_{chat.id}_{datetime.now().strftime('%Y%m%d')}.json"
                
            elif export_format == 'txt':
                # Export as plain text
                lines = []
                lines.append(f"Chat: {chat.name}")
                lines.append(f"Date: {chat.create_date.strftime('%Y-%m-%d %H:%M:%S')}")
                lines.append("-" * 80)
                
                for message in messages:
                    sender = "User" if message.message_type == 'user' else "AI"
                    timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
                    lines.append(f"{sender} ({timestamp}):")
                    lines.append(message.content)
                    lines.append("-" * 80)
                
                # Generate the file
                data = "\n".join(lines)
                filename = f"chat_{chat.id}_{datetime.now().strftime('%Y%m%d')}.txt"
                
            elif export_format == 'markdown':
                # Export as Markdown
                lines = []
                lines.append(f"# Chat: {chat.name}")
                lines.append(f"Date: {chat.create_date.strftime('%Y-%m-%d %H:%M:%S')}")
                lines.append("")
                
                for message in messages:
                    sender = "User" if message.message_type == 'user' else "AI"
                    timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
                    lines.append(f"## {sender} ({timestamp})")
                    lines.append(message.content)
                    lines.append("")
                
                # Generate the file
                data = "\n".join(lines)
                filename = f"chat_{chat.id}_{datetime.now().strftime('%Y%m%d')}.md"
                
            else:  # export_format == 'html'
                # Export as HTML
                lines = []
                lines.append("<!DOCTYPE html>")
                lines.append("<html>")
                lines.append("<head>")
                lines.append(f"<title>Chat: {chat.name}</title>")
                lines.append("<style>")
                lines.append("body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }")
                lines.append(".message { margin-bottom: 20px; padding: 10px; border-radius: 10px; }")
                lines.append(".user { background-color: #f0f0f0; text-align: right; }")
                lines.append(".assistant { background-color: #e6f7ff; }")
                lines.append(".system { background-color: #fff3cd; }")
                lines.append(".header { color: #666; font-size: 0.8em; margin-bottom: 5px; }")
                lines.append(".content { white-space: pre-wrap; }")
                lines.append("</style>")
                lines.append("</head>")
                lines.append("<body>")
                lines.append(f"<h1>Chat: {chat.name}</h1>")
                lines.append(f"<p>Date: {chat.create_date.strftime('%Y-%m-%d %H:%M:%S')}</p>")
                
                for message in messages:
                    msg_type = message.message_type
                    sender = "User" if msg_type == 'user' else ("AI" if msg_type == 'assistant' else "System")
                    timestamp = message.create_date.strftime('%Y-%m-%d %H:%M:%S')
                    
                    lines.append(f'<div class="message {msg_type}">')
                    lines.append(f'<div class="header">{sender} ({timestamp})</div>')
                    lines.append(f'<div class="content">{message.content}</div>')
                    lines.append('</div>')
                
                lines.append("</body>")
                lines.append("</html>")
                
                # Generate the file
                data = "\n".join(lines)
                filename = f"chat_{chat.id}_{datetime.now().strftime('%Y%m%d')}.html"
            
            # Return the file
            headers = [
                ('Content-Type', f'application/{export_format}'),
                ('Content-Disposition', content_disposition(filename))
            ]
            
            return request.make_response(data, headers=headers)
            
        except Exception as e:
            _logger.error(f"Error exporting chat: {str(e)}")
            return request.not_found()
            # Create a new chat session
            chat = request.env['ai.chat'].sudo().create({
                'name': post.get('name', f"Chat {datetime.now().strftime('%d/%m/%Y %H:%M')}"),
                'user_id': request.env.user.id,
                'company_id': request.env.company.id,
            })
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'chat_id': chat.id,
                    'name': chat.name,
                    'session_token': chat.session_token
                }),
                headers=[('Content-Type', 'application/json')]
            )
        except Exception as e:
            _logger.error(f"Error creating chat session: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')]
            )
    
    @http.route('/web/ai/chat/<int:chat_id>/message', type='http', auth='user', methods=['POST'], csrf=False)
    def send_message(self, chat_id, **post):
        """Send a message to the chat and get AI response"""
        try:
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': 'Chat not found or access denied'
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=404
                )
            
            # Get message content
            data = json.loads(request.httprequest.data.decode('utf-8'))
            message_content = data.get('message', '')
            model = data.get('model', None)  # Optional model override
            
            if not message_content:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': 'Message content is required'
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=400
                )
            
            # Send message and get response
            result = chat.send_message(message_content, model)
            
            if 'error' in result:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': result['error']
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=500
                )
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'response': result['response']
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error sending message: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/chat/<int:chat_id>/messages', type='http', auth='user', methods=['GET'], csrf=False)
    def get_chat_messages(self, chat_id, **params):
        """Get all messages for a chat"""
        try:
            # Get the chat
            chat = request.env['ai.chat'].sudo().browse(chat_id)
            
            # Check if chat exists and belongs to the user
            if not chat.exists() or chat.user_id.id != request.env.user.id:
                return request.make_response(
                    json.dumps({
                        'success': False,
                        'error': 'Chat not found or access denied'
                    }),
                    headers=[('Content-Type', 'application/json')],
                    status=404
                )
            
            # Get messages
            messages = chat.message_ids.sorted('create_date')
            
            result = []
            for message in messages:
                result.append({
                    'id': message.id,
                    'message_id': message.message_uuid,
                    'content': message.content,
                    'type': message.message_type,
                    'model_used': message.model_used,
                    'timestamp': message.create_date.isoformat(),
                    'token_count': message.token_count or 0,
                })
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'chat': {
                        'id': chat.id,
                        'name': chat.name,
                        'user_id': chat.user_id.id,
                        'created_at': chat.create_date.isoformat(),
                        'last_message': chat.last_message_date.isoformat() if chat.last_message_date else None,
                    },
                    'messages': result
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error getting chat messages: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )
    
    @http.route('/web/ai/chat/list', type='http', auth='user', methods=['GET'], csrf=False)
    def get_chat_list(self, **params):
        """Get list of all chats for the current user"""
        try:
            # Get all active chats for the user
            chats = request.env['ai.chat'].sudo().search([
                ('user_id', '=', request.env.user.id),
                ('active', '=', True)
            ], order='last_message_date desc')
            
            result = []
            for chat in chats:
                # Get the last message
                last_message = chat.message_ids.sorted('create_date', reverse=True)[:1]
                
                result.append({
                    'id': chat.id,
                    'name': chat.name,
                    'created_at': chat.create_date.isoformat(),
                    'last_message_date': chat.last_message_date.isoformat() if chat.last_message_date else None,
                    'last_message': last_message.content[:100] + '...' if last_message else None,
                    'total_messages': len(chat.message_ids),
                    'total_tokens': chat.total_tokens,
                    'topic': chat.topic or 'New Chat',
                    'summary': chat.summary or None,
                })
            
            return request.make_response(
                json.dumps({
                    'success': True,
                    'chats': result
                }),
                headers=[('Content-Type', 'application/json')]
            )
            
        except Exception as e:
            _logger.error(f"Error getting chat list: {str(e)}")
            return request.make_response(
                json.dumps({
                    'success': False,
                    'error': str(e)
                }),
                headers=[('Content-Type', 'application/json')],
                status=500
            )