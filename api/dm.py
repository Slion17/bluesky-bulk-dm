from http.server import BaseHTTPRequestHandler
import json
import traceback
import time
import re

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            # Set CORS headers first
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()
            
            # Read the request body
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            data = json.loads(post_data.decode('utf-8'))
            
            user_handle = data.get('userHandle')
            user_password = data.get('userPassword')
            target_handle = data.get('targetHandle')
            message = data.get('message')
            embedded_links = data.get('embeddedLinks', [])  # New: array of {text, url, start, end}
            
            if not all([user_handle, user_password, target_handle, message]):
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': 'Missing required fields (userHandle, userPassword, targetHandle, message)'
                }).encode())
                return
            
            # Validate message length
            if len(message) > 1000:
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': 'Message too long. Maximum 1000 characters allowed.'
                }).encode())
                return
            
            # Import atproto inside the try block to catch import errors
            from atproto import Client, models, client_utils
            from atproto.exceptions import RequestException
            
            # Login to Bluesky
            client = Client()
            try:
                client.login(user_handle, user_password)
            except Exception as e:
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': f'Authentication failed: {str(e)}',
                    'error_type': 'AuthenticationError'
                }).encode())
                return
            
            # Get profile of target user to get their DID
            try:
                profile = client.app.bsky.actor.get_profile({'actor': target_handle})
                target_did = profile.did
            except Exception as e:
                self.wfile.write(json.dumps({
                    'success': False,
                    'error': f'Could not find user {target_handle}: {str(e)}',
                    'error_type': 'UserNotFound'
                }).encode())
                return
            
            # Prepare message with rich text facets if embedded links are provided
            message_content = None
            if embedded_links:
                # Use TextBuilder for rich text with embedded links
                try:
                    text_builder = client_utils.TextBuilder()
                    
                    # Sort embedded links by start position to process in order
                    sorted_links = sorted(embedded_links, key=lambda x: x.get('start', 0))
                    
                    last_pos = 0
                    for link in sorted_links:
                        link_text = link.get('text', '')
                        link_url = link.get('url', '')
                        start_pos = link.get('start', 0)
                        end_pos = link.get('end', start_pos + len(link_text))
                        
                        if start_pos > last_pos:
                            # Add text before the link
                            text_builder.text(message[last_pos:start_pos])
                        
                        # Add the embedded link
                        text_builder.link(link_text, link_url)
                        last_pos = end_pos
                    
                    # Add any remaining text after the last link
                    if last_pos < len(message):
                        text_builder.text(message[last_pos:])
                    
                    message_content = text_builder
                    
                except Exception as e:
                    # Fallback to simple text if TextBuilder fails
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Rich text processing failed: {str(e)}. Using simple text instead.',
                        'error_type': 'RichTextError'
                    }).encode())
                    return
            else:
                # Simple text message with auto-detected links
                try:
                    # Try to auto-detect and parse links in the message
                    text_builder = client_utils.TextBuilder()
                    
                    # Auto-detect URLs in the text
                    url_pattern = r'https?://[^\s]+'
                    urls = list(re.finditer(url_pattern, message))
                    
                    if urls:
                        last_pos = 0
                        for match in urls:
                            # Add text before URL
                            if match.start() > last_pos:
                                text_builder.text(message[last_pos:match.start()])
                            
                            # Add the URL as a link
                            url = match.group()
                            text_builder.link(url, url)
                            last_pos = match.end()
                        
                        # Add any remaining text
                        if last_pos < len(message):
                            text_builder.text(message[last_pos:])
                        
                        message_content = text_builder
                    else:
                        # No URLs found, use simple text
                        message_content = message
                        
                except Exception:
                    # Fallback to simple text
                    message_content = message
            
            # Try to send the DM
            try:
                # Create DM client
                dm_client = client.with_bsky_chat_proxy()
                
                # Get or create conversation
                try:
                    convo_response = dm_client.chat.bsky.convo.get_convo_for_members(
                        models.ChatBskyConvoGetConvoForMembers.Params(members=[target_did])
                    )
                    convo_id = convo_response.convo.id
                except Exception as e:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Could not create conversation with {target_handle}. User may have DMs disabled or blocked you.',
                        'error_type': 'ConversationError'
                    }).encode())
                    return
                
                # Send the message with rich text support
                if isinstance(message_content, client_utils.TextBuilder):
                    # Send rich text message
                    message_data = models.ChatBskyConvoDefs.MessageInput(
                        text=message_content.build_text(),
                        facets=message_content.build_facets()
                    )
                else:
                    # Send simple text message
                    message_data = models.ChatBskyConvoDefs.MessageInput(text=message_content)
                
                send_response = dm_client.chat.bsky.convo.send_message(
                    models.ChatBskyConvoSendMessage.Data(
                        convo_id=convo_id,
                        message=message_data
                    )
                )
                
                self.wfile.write(json.dumps({
                    'success': True,
                    'message': f'Successfully sent DM to {target_handle}',
                    'target_handle': target_handle,
                    'rich_text_used': isinstance(message_content, client_utils.TextBuilder)
                }).encode())
                
            except Exception as e:
                error_message = str(e).lower()
                
                # Check for common DM-related errors
                if 'blocked' in error_message or 'block' in error_message:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Cannot send DM to {target_handle}: You may be blocked by this user',
                        'error_type': 'BlockedError'
                    }).encode())
                elif 'disabled' in error_message or 'not accepting' in error_message:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Cannot send DM to {target_handle}: User has DMs disabled',
                        'error_type': 'DMsDisabled'
                    }).encode())
                elif 'conversation' in error_message or 'convo' in error_message:
                    self.wfile.write(json.dumps({
                        'success': False,
                        'error': f'Cannot send DM to {target_handle}: Conversation error. User may not accept DMs from you.',
                        'error_type': 'ConversationError'
                    }).encode())
                else:
                    # Check for rate limiting
                    is_rate_limit = any([
                        "429" in str(e),
                        "rate" in error_message and "limit" in error_message,
                        "too many requests" in error_message,
                        "ratelimit" in error_message
                    ])
                    
                    if is_rate_limit:
                        self.wfile.write(json.dumps({
                            'success': False,
                            'error': 'Rate limit exceeded. Please slow down your requests.',
                            'error_type': 'RateLimit',
                            'retry_after': 60,
                            'target_handle': target_handle
                        }).encode())
                    else:
                        self.wfile.write(json.dumps({
                            'success': False,
                            'error': f'Failed to send DM to {target_handle}: {str(e)}',
                            'error_type': 'DMError',
                            'target_handle': target_handle
                        }).encode())
            
        except ImportError as e:
            # Handle missing dependencies
            self.wfile.write(json.dumps({
                'success': False,
                'error': f'Missing dependency: {str(e)}. Please ensure atproto library is installed.'
            }).encode())
            
        except json.JSONDecodeError as e:
            # Handle JSON parsing errors
            self.wfile.write(json.dumps({
                'success': False,
                'error': f'Invalid JSON data: {str(e)}'
            }).encode())
            
        except Exception as e:
            # Handle all other errors with detailed traceback
            error_details = {
                'success': False,
                'error': str(e),
                'error_type': type(e).__name__,
                'traceback': traceback.format_exc()
            }
            
            self.wfile.write(json.dumps(error_details).encode())
    
    def do_OPTIONS(self):
        # Handle preflight requests
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(b'')
