from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import string
from scenarios import SCENARIOS, ROLES_AR
import threading
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mafioso_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# Game State Storage (In-memory)
rooms = {}
# Timer threads tracking
active_timers = {}

def generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))

@app.route('/')
def index():
    return render_template('index.html')

# --- Helper Functions ---
def get_winner(room):
    """Check win conditions"""
    mafia_count = sum(1 for p in room['players'].values() if p['role'] == 'Mafioso' and p['alive'])
    town_count = sum(1 for p in room['players'].values() if p['role'] != 'Mafioso' and p['alive'])
    
    if mafia_count == 0:
        return 'Town'  # Town Wins
    if mafia_count >= town_count:
        return 'Mafia'  # Mafia Wins
    return None  # Game Continues

def broadcast_room_state(room_code):
    """Broadcast current room state to all players"""
    if room_code in rooms:
        socketio.emit('player_update', {'players': rooms[room_code]['players']}, room=room_code)

def stop_timer(room_code):
    """Stop any active timer for this room"""
    if room_code in active_timers:
        active_timers[room_code] = False

def run_timer(room_code, seconds):
    """Background task for countdown timer"""
    # Mark this timer as active
    active_timers[room_code] = True
    count = seconds
    
    while count > 0 and active_timers.get(room_code, False):
        time.sleep(1)
        count -= 1
        
        if room_code in rooms and active_timers.get(room_code, False):
            socketio.emit('timer_update', {'time': count}, room=room_code)
        else:
            return
    
    # Timer ended naturally
    if room_code in rooms and active_timers.get(room_code, False):
        socketio.emit('timer_end', {'message': 'انتهى الوقت! حان وقت التصويت'}, room=room_code)
        active_timers[room_code] = False

# --- Socket Events ---

@socketio.on('create_room')
def handle_create_room(data):
    """Create a new game room"""
    username = data['username']
    avatar = data.get('avatar', '👤')
    room_code = generate_room_code()
    
    # Ensure unique room code
    while room_code in rooms:
        room_code = generate_room_code()
    
    rooms[room_code] = {
        'players': {}, 
        'state': 'LOBBY', 
        'host': request.sid,
        'scenario': None,
        'round': 0,
        'votes': {},
        'config': {'time': 60, 'mafia_count': 1},
        'evidence_history': []
    }
    
    join_room(room_code)
    rooms[room_code]['players'][request.sid] = {
        'name': username,
        'role': 'Spectator',
        'character': 'Host',
        'alive': True,
        'is_host': True,
        'avatar': avatar
    }
    
    emit('room_created', {
        'room_code': room_code, 
        'players': rooms[room_code]['players']
    }, room=room_code)

@socketio.on('join_room')
def handle_join_room(data):
    """Join an existing game room"""
    username = data['username']
    room_code = data['room_code'].upper()
    avatar = data.get('avatar', '👤')
    
    if room_code not in rooms:
        emit('error', {'message': 'الغرفة غير موجودة'})
        return
        
    if rooms[room_code]['state'] != 'LOBBY':
        emit('error', {'message': 'اللعبة بدأت بالفعل'})
        return

    join_room(room_code)
    rooms[room_code]['players'][request.sid] = {
        'name': username,
        'role': 'Spectator',
        'character': 'Guest',
        'alive': True,
        'is_host': False,
        'avatar': avatar
    }
    
    broadcast_room_state(room_code)
    emit('join_success', {'room_code': room_code}, room=request.sid)

@socketio.on('start_game')
def handle_start_game(data):
    """Initialize and start the game"""
    room_code = data['room_code']
    mafia_count = int(data.get('mafia_count', 1))
    round_time = int(data.get('round_time', 60))
    
    if room_code not in rooms:
        return
        
    if rooms[room_code]['host'] != request.sid:
        emit('error', {'message': 'فقط المضيف يمكنه بدء اللعبة'})
        return
    
    player_sids = list(rooms[room_code]['players'].keys())
    
    if len(player_sids) < 3:
        emit('error', {'message': 'تحتاج 3 لاعبين على الأقل'})
        return
    
    if mafia_count >= len(player_sids):
        emit('error', {'message': 'عدد المافيا كثير جداً'})
        return
        
    # Save configuration
    rooms[room_code]['config']['time'] = round_time
    rooms[room_code]['config']['mafia_count'] = mafia_count
    
    # Select random scenario
    scenario = random.choice(SCENARIOS)
    rooms[room_code]['scenario'] = scenario
    
    # Prepare character pool
    character_pool = scenario['characters'][:]
    
    # Add generic characters if needed
    if len(character_pool) < len(player_sids):
        for i in range(len(player_sids) - len(character_pool)):
            character_pool.append({
                "name": f"مواطن {i+1}", 
                "bio": "شخص عادي لا علاقة له بالقضية مباشرة."
            })
    
    random.shuffle(character_pool)

    # Assign roles (Mafia vs Civilian)
    roles = ['Mafioso'] * mafia_count + ['Civilian'] * (len(player_sids) - mafia_count)
    random.shuffle(roles)
    
    # Assign roles and characters to players
    for i, sid in enumerate(player_sids):
        p = rooms[room_code]['players'][sid]
        p['role'] = roles[i]
        
        assigned_char = character_pool[i]
        p['character'] = assigned_char['name']
        p['character_bio'] = assigned_char['bio']
        p['alive'] = True
        
        # Send private role information to each player
        role_info = ROLES_AR[roles[i]]
        socketio.emit('game_started', {
            'role_name': role_info['name'],
            'role_desc': role_info['description'],
            'character': p['character'],
            'character_bio': p['character_bio']
        }, room=sid)

    # Start first round
    start_new_round(room_code)

def start_new_round(room_code):
    """Start a new discussion round"""
    if room_code not in rooms:
        print(f"ERROR: Room {room_code} no longer exists")
        return
        
    room = rooms[room_code]
    room['round'] += 1
    current_round_idx = room['round'] - 1
    
    print(f"Starting Round {room['round']} in room {room_code}")
    
    # Get clue for this round
    clues = room['scenario']['clues']
    if current_round_idx < len(clues):
        new_clue = clues[current_round_idx]
    else:
        # Generic hints when out of scenario clues
        generic_hints = [
            "راقب لغة الجسد.. الكاذب يتجنب النظر في العين.",
            "المافيا قد يكون أهدأ شخص في الغرفة.",
            "دقق في كلام من يحاول اتهام الآخرين بسرعة.",
            "ليس كل من يصمت بريء..",
            "راجع الأدلة القديمة، قد تجد رابطاً مفقوداً.",
            "انتبه لمن يحاول تغيير الموضوع.",
            "الحقيقة دائماً في التفاصيل الصغيرة.",
            "من يدافع عن المتهم بشدة قد يكون شريكه."
        ]
        new_clue = random.choice(generic_hints)
    
    room['evidence_history'].append(new_clue)
    room['state'] = 'DAY'
    room['votes'] = {}  # Reset votes for new round
    
    # Stop any previous timer
    stop_timer(room_code)
    
    # Broadcast new round information
    socketio.emit('round_start', {
        'round_num': room['round'],
        'title': room['scenario']['title'],
        'evidence': new_clue,
        'history': room['evidence_history'],
        'timer': room['config']['time']
    }, room=room_code)
    
    print(f"Round {room['round']} started, starting timer for {room['config']['time']} seconds")
    
    # Start new timer in background thread
    timer_thread = threading.Thread(
        target=run_timer, 
        args=(room_code, room['config']['time']),
        daemon=True
    )
    timer_thread.start()

@socketio.on('cast_vote')
def handle_vote(data):
    """Handle player vote"""
    room_code = data['room_code']
    target_sid = data['target_sid']
    
    if room_code not in rooms:
        return
        
    room = rooms[room_code]
    
    # Check if voter exists and is alive
    voter = room['players'].get(request.sid)
    if not voter:
        return
        
    if not voter.get('alive', False):
        socketio.emit('error', {'message': 'لا يمكن للاعبين الميتين التصويت'}, room=request.sid)
        return
    
    # Check if target exists and is alive
    target = room['players'].get(target_sid)
    if not target:
        socketio.emit('error', {'message': 'اللاعب المستهدف غير موجود'}, room=request.sid)
        return
        
    if not target.get('alive', False):
        socketio.emit('error', {'message': 'لا يمكن التصويت على لاعب ميت'}, room=request.sid)
        return
    
    # Register vote
    room['votes'][request.sid] = target_sid
    
    # Count alive players
    alive_players = [pid for pid, p in room['players'].items() if p['alive']]
    votes_cast = len(room['votes'])
    
    # Calculate vote breakdown
    votes_breakdown = {}
    for voter_sid, target_id in room['votes'].items():
        if target_id in room['players']:
            target_name = room['players'][target_id]['name']
            votes_breakdown[target_id] = {
                'count': votes_breakdown.get(target_id, {}).get('count', 0) + 1,
                'name': target_name
            }

    socketio.emit('vote_update', {
        'votes_count': votes_cast, 
        'total_alive': len(alive_players),
        'breakdown': votes_breakdown
    }, room=room_code)
    
    # Confirm vote to the voter
    socketio.emit('vote_confirmed', {
        'target_name': target['name'],
        'message': f'تم تسجيل تصويتك ضد {target["name"]}'
    }, room=request.sid)
    
    # Check if all alive players voted
    if votes_cast >= len(alive_players):
        print(f"All players voted in room {room_code}, tallying votes...")
        tally_votes(room_code)

def tally_votes(room_code):
    """Count votes and eliminate player with most votes"""
    if room_code not in rooms:
        return
        
    room = rooms[room_code]
    
    # Stop the timer
    stop_timer(room_code)
    
    vote_counts = {}
    
    # Count votes
    for voter, target in room['votes'].items():
        vote_counts[target] = vote_counts.get(target, 0) + 1
    
    if not vote_counts:
        print("No votes cast, skipping elimination")
        # Start next round
        socketio.sleep(3)
        start_new_round(room_code)
        return
    
    # Find player with most votes
    kicked_sid = max(vote_counts, key=vote_counts.get)
    kicked_player = room['players'][kicked_sid]
    
    # Eliminate player
    kicked_player['alive'] = False
    
    print(f"Player {kicked_player['name']} eliminated with {vote_counts[kicked_sid]} votes")
    
    # Broadcast updated player list immediately so dead players disappear from voting UI
    broadcast_room_state(room_code)
    
    # Reveal eliminated player info
    socketio.emit('player_kicked', {
        'name': kicked_player['name'],
        'character': kicked_player['character'],
        'role': kicked_player['role'],
        'role_name': ROLES_AR[kicked_player['role']]['name'],
        'votes': vote_counts[kicked_sid]
    }, room=room_code)
    
    # Check for win condition
    winner = get_winner(room)
    
    if winner:
        mafia_count = sum(1 for p in room['players'].values() if p['role'] == 'Mafioso' and p['alive'])
        town_count = sum(1 for p in room['players'].values() if p['role'] != 'Mafioso' and p['alive'])
        
        if winner == 'Mafia':
            message = f"سيطرت المافيا على المدينة! (المافيا: {mafia_count} - الأبرياء: {town_count})"
        else:
            message = "انتصرت العدالة! تم القبض على جميع أفراد المافيا!"
        
        socketio.emit('game_over', {
            'winner': 'المافيا' if winner == 'Mafia' else 'الأبرياء',
            'message': message,
            'players': room['players']
        }, room=room_code)
        
        room['state'] = 'END'
        stop_timer(room_code)
        
        print(f"Game Over in room {room_code}. Winner: {winner}")
    else:
        # Continue to next round after delay
        print(f"No winner yet in room {room_code}, continuing to next round...")
        
        def delayed_next_round():
            time.sleep(5)
            if room_code in rooms:
                start_new_round(room_code)
        
        threading.Thread(target=delayed_next_round, daemon=True).start()

@socketio.on('disconnect')
def handle_disconnect():
    """Handle player disconnection"""
    for room_code, room_data in list(rooms.items()):
        if request.sid in room_data['players']:
            player_name = room_data['players'][request.sid]['name']
            
            # Remove player
            del room_data['players'][request.sid]
            
            print(f"Player {player_name} disconnected from room {room_code}")
            
            # If room is empty, delete it
            if not room_data['players']:
                stop_timer(room_code)
                del rooms[room_code]
                print(f"Room {room_code} deleted (empty)")
            else:
                # Broadcast updated player list
                broadcast_room_state(room_code)
                
                # If game is running, check win condition
                if room_data['state'] in ['DAY', 'NIGHT']:
                    winner = get_winner(room_data)
                    if winner:
                        socketio.emit('game_over', {
                            'winner': 'المافيا' if winner == 'Mafia' else 'الأبرياء',
                            'message': 'انتهت اللعبة بسبب خروج لاعبين',
                            'players': room_data['players']
                        }, room=room_code)
                        room_data['state'] = 'END'
            break

@socketio.on('request_game_state')
def handle_request_state(data):
    """Send current game state to reconnecting player"""
    room_code = data.get('room_code')
    if room_code and room_code in rooms:
        room = rooms[room_code]
        emit('game_state_update', {
            'state': room['state'],
            'round': room['round'],
            'players': room['players'],
            'evidence_history': room['evidence_history']
        })

if __name__ == '__main__':
    print("Starting Mafia Game Server...")
    print("Server running on http://localhost:5000")
    socketio.run(app, debug=True, allow_unsafe_werkzeug=True)